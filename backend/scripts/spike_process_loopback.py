#!/usr/bin/env python3
"""Spike: WASAPI per-process loopback capture (Windows 10 2004+) from Python.

Verification-first spike for OpenSpec change ``add-per-process-audio-capture``.
It proves, on the *real* machine it runs on, that:

  * ``ActivateAudioInterfaceAsync`` accepts the literal device string
    ``VAD\\Process_Loopback`` (a GUID string fails with 0x8000000E);
  * the completion handler must implement BOTH
    ``IActivateAudioInterfaceCompletionHandler`` and ``IAgileObject``;
  * capture must run on an MTA thread (``CoInitializeEx(COINIT_MULTITHREADED)``);
  * the engine delivers buffers in the *requested* format (48 kHz / stereo /
    float32), not ``GetMixFormat``;
  * ``AUDCLNT_BUFFERFLAGS_SILENT`` (0x2) packets are observable;
  * the same PID can be re-activated after fully releasing all interfaces
    (including the ``IActivateAudioInterfaceAsyncOperation``), and documents
    the failure mode when the async operation is *not* released.

It spawns a child Python process that plays audible 440 Hz tones (using the
already-present ``soundcard`` dependency) so a real render session exists.

Usage::

    python backend/scripts/spike_process_loopback.py
    python backend/scripts/spike_process_loopback.py --duration 3 --keep-wav

Outputs ``backend/scripts/spike_out.wav`` for manual listening.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import os
import platform
import subprocess
import sys
import threading
import time
import wave
from ctypes import (
    POINTER,
    byref,
    c_int32,
    c_longlong,
    c_ubyte,
    c_uint32,
    c_uint64,
    c_void_p,
    c_wchar_p,
)
from ctypes import wintypes

import numpy as np

# --------------------------------------------------------------------------- #
# HRESULT / constants
# --------------------------------------------------------------------------- #

_HRESULT_NAMES = {
    0x00000000: "S_OK",
    0x00000001: "S_FALSE",
    0x80004001: "E_NOTIMPL",
    0x80004002: "E_NOINTERFACE",
    0x80004003: "E_POINTER",
    0x80004004: "E_ABORT",
    0x80004005: "E_FAIL",
    0x8000000E: "E_ILLEGAL_METHOD_CALL",
    0x80070005: "E_ACCESSDENIED",
    0x80070057: "E_INVALIDARG",
    0x80070490: "ERROR_NOT_FOUND",
    0x8007000E: "E_OUTOFMEMORY",
    0x80010106: "RPC_E_CHANGED_MODE",
    0x88890001: "AUDCLNT_E_NOT_INITIALIZED",
    0x88890002: "AUDCLNT_E_ALREADY_INITIALIZED",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED",
    0x88890005: "AUDCLNT_E_NOT_STOPPED",
    0x88890008: "AUDCLNT_E_OUT_OF_ORDER",
    0x88890009: "AUDCLNT_E_UNSUPPORTED_FORMAT",
    0x8889000B: "AUDCLNT_E_DEVICE_IN_USE",
    0x88890021: "AUDCLNT_E_RESOURCES_INVALIDATED",
}

E_ILLEGAL_METHOD_CALL = 0x8000000E

# Literal device-interface path — NOT a GUID. See mmdeviceapi.h:
#   #define VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK L"VAD\\Process_Loopback"
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0

AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_SHAREMODE_SHARED = 0

WAVE_FORMAT_IEEE_FLOAT = 0x0003
AUDCLNT_BUFFERFLAGS_SILENT = 0x2

COINIT_APARTMENTTHREADED = 0x2
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = 0x80010106

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

REQUESTED_RATE = 48000
REQUESTED_CHANNELS = 2
REQUESTED_BITS = 32
REQUESTED_FRAME_BYTES = REQUESTED_CHANNELS * REQUESTED_BITS // 8


def hr_hex(hr: int) -> str:
    return f"0x{hr & 0xFFFFFFFF:08X}"


def hr_name(hr: int) -> str:
    u = hr & 0xFFFFFFFF
    return f"{_HRESULT_NAMES.get(u, 'UNKNOWN')} ({hr_hex(u)})"


# --------------------------------------------------------------------------- #
# Structs / COM
# --------------------------------------------------------------------------- #

import comtypes  # noqa: E402  (import after stdlib for clarity)
from comtypes import COMMETHOD, GUID, IUnknown  # noqa: E402


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", wintypes.DWORD),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", wintypes.DWORD),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_uint32), ("pBlobData", c_void_p)]


class _PROPVARIANT(ctypes.Structure):
    """PROPVARIANT carrying a VT_BLOB (24 bytes on 64-bit Windows)."""

    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("wReserved1", ctypes.c_ushort),
        ("wReserved2", ctypes.c_ushort),
        ("wReserved3", ctypes.c_ushort),
        ("blob", _BLOB),
    ]


def make_format_ieee_float(rate=48000, channels=2, bits=32) -> WAVEFORMATEX:
    fmt = WAVEFORMATEX()
    fmt.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
    fmt.nChannels = channels
    fmt.nSamplesPerSec = rate
    fmt.wBitsPerSample = bits
    fmt.nBlockAlign = (channels * bits) // 8
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


IID_IActivateAudioInterfaceCompletionHandler = GUID(
    "{41D949AB-9862-444A-80F6-C261334DA5EB}"
)
IID_IActivateAudioInterfaceAsyncOperation = GUID(
    "{72A22D78-CDE4-431D-B8CC-843A71199B6D}"
)
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IAgileObject = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")

REFERENCE_TIME = c_longlong


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = IID_IActivateAudioInterfaceAsyncOperation
    _methods_ = [
        COMMETHOD(
            [], comtypes.HRESULT, "GetActivateResult",
            (["out"], POINTER(comtypes.HRESULT), "activateResult"),
            (["out"], POINTER(POINTER(IUnknown)), "activatedInterface"),
        ),
    ]


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = IID_IActivateAudioInterfaceCompletionHandler
    _methods_ = [
        COMMETHOD(
            [], comtypes.HRESULT, "ActivateCompleted",
            (["in"], POINTER(IActivateAudioInterfaceAsyncOperation),
             "activateOperation"),
        ),
    ]


class IAgileObject(IUnknown):
    """Apartment-neutral marker interface. Required on the completion handler."""

    _iid_ = IID_IAgileObject
    _methods_ = []


class IAudioClient(IUnknown):
    _iid_ = IID_IAudioClient
    _methods_ = [
        COMMETHOD([], comtypes.HRESULT, "Initialize",
                  (["in"], c_uint32, "ShareMode"),
                  (["in"], c_uint32, "StreamFlags"),
                  (["in"], REFERENCE_TIME, "hnsBufferDuration"),
                  (["in"], REFERENCE_TIME, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], comtypes.HRESULT, "GetBufferSize",
                  (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], comtypes.HRESULT, "GetStreamLatency",
                  (["out"], POINTER(REFERENCE_TIME), "phnsLatency")),
        COMMETHOD([], comtypes.HRESULT, "GetCurrentPadding",
                  (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
        COMMETHOD([], comtypes.HRESULT, "IsFormatSupported",
                  (["in"], c_uint32, "ShareMode"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], comtypes.HRESULT, "GetMixFormat",
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], comtypes.HRESULT, "GetDevicePeriod",
                  (["out"], POINTER(REFERENCE_TIME), "phnsDefaultDevicePeriod"),
                  (["out"], POINTER(REFERENCE_TIME), "phnsMinimumDevicePeriod")),
        COMMETHOD([], comtypes.HRESULT, "Start"),
        COMMETHOD([], comtypes.HRESULT, "Stop"),
        COMMETHOD([], comtypes.HRESULT, "Reset"),
        COMMETHOD([], comtypes.HRESULT, "SetEventHandle",
                  (["in"], wintypes.HANDLE, "eventHandle")),
        COMMETHOD([], comtypes.HRESULT, "GetService",
                  (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(c_void_p), "ppv")),
    ]


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = [
        COMMETHOD([], comtypes.HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(c_uint32), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(c_uint64), "pu64QPCPosition")),
        COMMETHOD([], comtypes.HRESULT, "ReleaseBuffer",
                  (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], comtypes.HRESULT, "GetNextPacketSize",
                  (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    ]


class _CompletionHandler(comtypes.COMObject):
    """Signals a Win32 event on activation completion.

    Implements BOTH interfaces. Missing IAgileObject makes
    ActivateAudioInterfaceAsync fail with E_ILLEGAL_METHOD_CALL (0x8000000E).
    """

    _com_interfaces_ = [
        IActivateAudioInterfaceCompletionHandler,
        IAgileObject,
    ]

    def __init__(self, event_handle):
        super().__init__()
        self._event = event_handle

    def ActivateCompleted(self, activate_operation):
        ctypes.windll.kernel32.SetEvent(self._event)
        return 0


class _NonAgileHandler(comtypes.COMObject):
    """Negative control: omits IAgileObject on purpose."""

    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler]

    def __init__(self, event_handle):
        super().__init__()
        self._event = event_handle

    def ActivateCompleted(self, activate_operation):
        ctypes.windll.kernel32.SetEvent(self._event)
        return 0


def query_apartment() -> str:
    apt = ctypes.c_int(0)
    qual = ctypes.c_int(0)
    fn = ctypes.windll.ole32.CoGetApartmentType
    fn.restype = ctypes.c_long
    fn.argtypes = [POINTER(ctypes.c_int), POINTER(ctypes.c_int)]
    hr = fn(byref(apt), byref(qual))
    if hr != 0:
        return f"<CoGetApartmentType hr={hr_hex(hr)}>"
    names = {-1: "CURRENT", 0: "STA", 1: "MTA", 2: "NA", 3: "MAIN_STA"}
    return f"{names.get(apt.value, apt.value)} (qualifier={qual.value})"


class Activation:
    """Owns everything returned by a successful activation.

    comtypes releases COM pointers in ``__del__``; ``release()`` drops the
    Python references and forces GC so the refcounts actually drop. Set
    ``keep_operation=True`` to deliberately leak the async operation (used as
    the regression baseline).
    """

    def __init__(self):
        self.audio_client = None
        self.capture_client = None
        self.activated = None
        self.operation = None
        self.handler = None
        self.handler_ptr = None
        self.capture_event = None
        self.format = None
        self.mix_format = None
        self.info = {}

    def release(self, keep_operation: bool = False):
        if self.capture_event:
            ctypes.windll.kernel32.CloseHandle(self.capture_event)
            self.capture_event = None
        self.audio_client = None
        self.capture_client = None
        self.activated = None
        self.handler = None
        self.handler_ptr = None
        if not keep_operation:
            self.operation = None
        gc.collect()


LEAKED_OPERATIONS = []  # regression baseline: intentionally-unreleased async ops


def activate_process_loopback(
    pid: int,
    *,
    device: str = VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
    request_format=(REQUESTED_RATE, REQUESTED_CHANNELS, REQUESTED_BITS),
    hns_buffer: int = 0,
    handler_cls=_CompletionHandler,
    timeout_ms: int = 5000,
):
    """Activate + Initialize a process-loopback IAudioClient for ``pid``.

    Must be called on an MTA-initialized thread. Returns ``(Activation|None, hr)``.
    """
    act = Activation()
    act.info["device"] = device
    act.info["hns_buffer_requested"] = hns_buffer
    act.info["handler_interfaces"] = [
        i.__name__ for i in handler_cls._com_interfaces_
    ]

    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = pid
    params.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    )

    pv = _PROPVARIANT()
    pv.vt = 0x41  # VT_BLOB
    pv.blob.cbSize = ctypes.sizeof(params)
    pv.blob.pBlobData = ctypes.cast(ctypes.addressof(params), c_void_p)

    completion_event = ctypes.windll.kernel32.CreateEventW(None, True, False, None)
    if not completion_event:
        return act, 0x80004005

    handler = handler_cls(completion_event)
    act.handler = handler

    try:
        handler_ptr = handler.QueryInterface(
            IActivateAudioInterfaceCompletionHandler
        )
    except comtypes.COMError as ce:
        ctypes.windll.kernel32.CloseHandle(completion_event)
        return act, ce.hresult & 0xFFFFFFFF
    act.handler_ptr = handler_ptr

    mmdev = ctypes.WinDLL("Mmdevapi.dll")
    ActivateAudioInterfaceAsync = mmdev.ActivateAudioInterfaceAsync
    ActivateAudioInterfaceAsync.restype = ctypes.c_long
    ActivateAudioInterfaceAsync.argtypes = [
        c_wchar_p, c_void_p, c_void_p, c_void_p, c_void_p,
    ]

    operation_ptr = c_void_p(0)
    pv_addr = ctypes.cast(ctypes.pointer(pv), c_void_p).value
    iid_addr = ctypes.cast(ctypes.pointer(IID_IAudioClient), c_void_p).value
    handler_raw = ctypes.cast(handler_ptr, c_void_p).value

    try:
        hr = ActivateAudioInterfaceAsync(
            device, iid_addr, pv_addr, handler_raw,
            ctypes.cast(ctypes.pointer(operation_ptr), c_void_p),
        )
        hr_u32 = hr & 0xFFFFFFFF
        act.info["activate_async_hr"] = hr_u32
        if hr_u32 != 0:
            return act, hr_u32

        operation = ctypes.cast(
            operation_ptr, POINTER(IActivateAudioInterfaceAsyncOperation)
        )
        act.operation = operation

        wait = ctypes.windll.kernel32.WaitForSingleObject(
            completion_event, timeout_ms
        )
        act.info["completion_wait"] = wait
        if wait != WAIT_OBJECT_0:
            return act, 0x80004005

        activate_hr, activated = operation.GetActivateResult()
        act.info["get_activate_result_hr"] = (
            activate_hr & 0xFFFFFFFF if activate_hr else 0
        )
        if activate_hr != 0 or not activated:
            return act, (activate_hr & 0xFFFFFFFF if activate_hr else 0x80004003)

        act.activated = activated
        try:
            audio_client = activated.QueryInterface(IAudioClient)
        except comtypes.COMError as ce:
            act.info["query_interface_hr"] = ce.hresult & 0xFFFFFFFF
            return act, ce.hresult & 0xFFFFFFFF
        act.audio_client = audio_client
    finally:
        ctypes.windll.kernel32.CloseHandle(completion_event)

    try:
        mix_ptr = audio_client.GetMixFormat()
        mix = mix_ptr.contents
        act.mix_format = (int(mix.nSamplesPerSec), int(mix.nChannels),
                          int(mix.wBitsPerSample), int(mix.wFormatTag))
    except Exception as exc:  # noqa: BLE001
        act.mix_format = f"<GetMixFormat failed: {exc}>"

    rate, channels, bits = request_format
    requested = make_format_ieee_float(rate, channels, bits)
    act.format = (rate, channels, bits)
    act.info["requested_format"] = (rate, channels, bits)
    act.info["mix_format"] = act.mix_format

    flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
    act.info["init_flags"] = flags

    last_hr = 0
    for buf_dur in (hns_buffer, 2_000_000):
        try:
            audio_client.Initialize(
                AUDCLNT_SHAREMODE_SHARED, flags, buf_dur, 0,
                byref(requested), None,
            )
            act.info["hns_buffer_used"] = buf_dur
            last_hr = 0
            break
        except comtypes.COMError as ce:
            last_hr = ce.hresult & 0xFFFFFFFF
            act.info[f"init_hr_buf_{buf_dur}"] = last_hr
    if last_hr != 0:
        return act, last_hr

    try:
        capture_void = audio_client.GetService(byref(IID_IAudioCaptureClient))
        capture_client = ctypes.cast(capture_void, POINTER(IAudioCaptureClient))
        act.capture_client = capture_client
    except comtypes.COMError as ce:
        return act, ce.hresult & 0xFFFFFFFF

    capture_event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
    act.capture_event = capture_event
    try:
        audio_client.SetEventHandle(capture_event)
        audio_client.Start()
    except comtypes.COMError as ce:
        return act, ce.hresult & 0xFFFFFFFF

    return act, 0


class CaptureResult:
    def __init__(self):
        self.ok = False
        self.hr = 0
        self.exception = None
        self.info = {}
        self.packets = 0
        self.frames = 0
        self.bytes = 0
        self.silent_packets = 0
        self.silent_frames = 0
        self.rms = 0.0
        self.peak = 0.0
        self.raw = bytearray()
        self.duration_s = 0.0
        self.flag_histogram = {}
        self.target_exited_mid = False

    def release(self, keep_operation=False):
        if getattr(self, "_act", None):
            self._act.release(keep_operation=keep_operation)


def capture(
    pid: int,
    seconds: float,
    *,
    device: str = VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
    coinit_mode: int = COINIT_MULTITHREADED,
    do_coinit: bool = True,
    hns_buffer: int = 0,
    handler_cls=_CompletionHandler,
    collect: bool = True,
    timeout_ms: int = 5000,
    kill_after: float | None = None,
    kill_fn=None,
) -> CaptureResult:
    """Run activation + read loop on a fresh thread; return a CaptureResult."""
    result = CaptureResult()

    def worker():
        if do_coinit:
            hr = ctypes.windll.ole32.CoInitializeEx(None, coinit_mode)
            result.info["coinit_hr"] = hr & 0xFFFFFFFF
        result.info["apartment"] = query_apartment()

        try:
            act, hr = activate_process_loopback(
                pid, device=device, hns_buffer=hns_buffer,
                handler_cls=handler_cls, timeout_ms=timeout_ms,
            )
        except Exception as exc:  # noqa: BLE001
            result.exception = repr(exc)
            result.ok = False
            return

        result.info.update(act.info if act else {})
        result.hr = hr
        result._act = act
        if act is None or hr != 0 or act.capture_client is None:
            result.ok = False
            return

        capture_client = act.capture_client
        frame_bytes = act.format[1] * act.format[2] // 8
        if kill_fn is not None and kill_after is not None:
            def _late_kill():
                time.sleep(kill_after)
                result.target_exited_mid = True
                kill_fn()
            threading.Thread(target=_late_kill, name="late-kill", daemon=True).start()
        try:
            t_end = time.perf_counter() + seconds
            while time.perf_counter() < t_end:
                wait = ctypes.windll.kernel32.WaitForSingleObject(
                    act.capture_event, 200
                )
                if wait == WAIT_TIMEOUT:
                    continue
                if wait != WAIT_OBJECT_0:
                    result.info["wait_fail"] = wait
                    break
                while True:
                    nps = capture_client.GetNextPacketSize()
                    if not nps:
                        break
                    data_ptr, frames, flags, _dev, _qpc = (
                        capture_client.GetBuffer()
                    )
                    byte_count = int(frames) * frame_bytes
                    if flags & AUDCLNT_BUFFERFLAGS_SILENT:
                        raw = b"\x00" * byte_count
                        result.silent_packets += 1
                        result.silent_frames += int(frames)
                    else:
                        raw = ctypes.string_at(data_ptr, byte_count)
                    capture_client.ReleaseBuffer(frames)
                    result.flag_histogram[flags] = result.flag_histogram.get(flags, 0) + 1
                    result.packets += 1
                    result.frames += int(frames)
                    result.bytes += byte_count
                    if collect and len(result.raw) < 48_000 * 8 * 30:
                        result.raw.extend(raw)
                    if raw:
                        arr = np.frombuffer(raw, dtype="<f4")
                        if arr.size:
                            result.peak = max(result.peak, float(np.max(np.abs(arr))))
                            result._sumsq = getattr(result, "_sumsq", 0.0) + float(
                                np.sum(arr.astype(np.float64) ** 2)
                            )
                    if time.perf_counter() >= t_end:
                        break
            samples = result.frames * act.format[1]
            if samples:
                result.rms = (getattr(result, "_sumsq", 0.0) / samples) ** 0.5
            act.audio_client.Stop()
            result.ok = True
        except Exception as exc:  # noqa: BLE001
            result.exception = repr(exc)
            result.ok = False
        finally:
            # keep_operation intentionally NOT set here; caller decides
            pass

    t = threading.Thread(target=worker, name="spike-capture")
    t.start()
    t.join()
    result.duration_s = seconds
    return result


# --------------------------------------------------------------------------- #
# Tone player (child process, real render session)
# --------------------------------------------------------------------------- #

TONE_CODE = r"""
import sys, time
import numpy as np
import soundcard as sc

sr = 48000
spk = sc.default_speaker()
print("TONE_PLAYER_READY %r" % (spk.name,), flush=True)

def tone(sec):
    t = np.arange(int(sr * sec)) / sr
    x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    spk.play(np.column_stack([x, x]), samplerate=sr)

for i in range(30):
    print("PHASE tone_%d" % i, flush=True)
    tone(5)
    print("PHASE gap_%d" % i, flush=True)
    time.sleep(4)
print("PHASE exit", flush=True)
"""


class TonePlayer:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-c", TONE_CODE],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.lines: list[str] = []
        self._lock = threading.Lock()
        self._new = threading.Event()
        self._reader = threading.Thread(
            target=self._read, name="tone-reader", daemon=True
        )
        self._reader.start()

    def _read(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.rstrip()
            with self._lock:
                self.lines.append(line)
            self._new.set()
            print(f"[TONE] {line}", flush=True)

    def wait_ready(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if any(l.startswith("TONE_PLAYER_READY") for l in self.lines):
                    return True
            time.sleep(0.1)
        return False

    def wait_next_gap(self, timeout=30):
        """Wait until a PHASE gap_N line appears that we have not seen before."""
        with self._lock:
            seen = sum(1 for l in self.lines if l.startswith("PHASE gap_"))
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                now = sum(1 for l in self.lines if l.startswith("PHASE gap_"))
            if now > seen:
                return True
            time.sleep(0.05)
        return False

    def kill(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        print(f"[TONE] killed pid={self.proc.pid}", flush=True)


# --------------------------------------------------------------------------- #
# Session enumeration
# --------------------------------------------------------------------------- #

def enumerate_sessions(label: str):
    from pycaw.pycaw import AudioUtilities
    import psutil

    sessions = AudioUtilities.GetAllSessions()
    print(f"\n[{label}] session count = {len(sessions)}")
    for s in sessions:
        pid = s.ProcessId
        try:
            state = s.State
        except Exception as exc:  # noqa: BLE001
            state = f"<err {exc}>"
        # pycaw's high-level AudioSession no longer exposes
        # IsSystemSoundsSession(); the underlying IAudioSessionControl2 does.
        try:
            syssnd = bool(s._ctl.IsSystemSoundsSession() == 0)
        except Exception as exc:  # noqa: BLE001
            syssnd = f"<err {type(exc).__name__}: {exc}>"
        try:
            disp = s.DisplayName
        except Exception as exc:  # noqa: BLE001
            disp = f"<err {exc}>"
        name = None
        if pid == 0:
            name = "<PID 0>"
        else:
            try:
                name = psutil.Process(pid).name()
            except Exception as exc:  # noqa: BLE001
                name = f"<{type(exc).__name__}>"
        state_name = {0: "Inactive", 1: "Active", 2: "Expired"}.get(state, state)
        print(
            f"  PID={pid:<7} State={state_name:<8} SysSounds={syssnd!s:<6} "
            f"Name={name!r} DisplayName={disp!r}"
        )
    return sessions


def find_pid(sessions, pid):
    return [s for s in sessions if s.ProcessId == pid]


# --------------------------------------------------------------------------- #
# Report helpers
# --------------------------------------------------------------------------- #

REPORT: list[tuple[str, str, str]] = []  # (id, status, note)


def record(item: str, passed: bool, note: str = ""):
    REPORT.append((item, "PASS" if passed else "FAIL", note))
    print(f"  >>> {item}: {'PASS' if passed else 'FAIL'} {note}", flush=True)


def dump_wav(path: str, raw: bytes, rate=48000, channels=2):
    arr = np.frombuffer(raw, dtype="<f4")
    if arr.size == 0:
        print(f"  (no audio to write to {path})")
        return
    arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = (np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())
    print(f"  wrote {path} ({len(pcm)} samples, {len(raw)} raw bytes)")


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=4.0)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "spike_out.wav"))
    ap.add_argument("--keep-wav", action="store_true")
    args = ap.parse_args()

    print("=" * 78)
    print("SPIKE: WASAPI per-process loopback capture")
    print("=" * 78)
    print(f"[ENV] python      = {sys.version.split()[0]} ({sys.executable})")
    print(f"[ENV] platform    = {platform.platform()}")
    print(f"[ENV] winversion  = {sys.getwindowsversion()}")
    build = sys.getwindowsversion().build
    print(f"[ENV] build       = {build} (gate >= 19041: {build >= 19041})")
    print(f"[ENV] comtypes    = {comtypes.__version__}")
    try:
        import pycaw
        print(f"[ENV] pycaw       = {getattr(pycaw, '__version__', 'unknown')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ENV] pycaw import failed: {exc}")
    import psutil
    print(f"[ENV] psutil      = {psutil.__version__}")
    print(f"[ENV] literal device string = {VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK!r}")
    record("g. Windows build >= 19041", build >= 19041, f"build={build}")

    enumerate_sessions("SESSIONS-BASELINE")

    player = TonePlayer()
    try:
        if not player.wait_ready(timeout=15):
            print("[FATAL] tone player did not become ready")
            print("[FATAL] (no audio output device? see soundcard probe)")
            return 2
        tone_pid = player.proc.pid
        print(f"\n[TONE] target PID = {tone_pid}")

        # wait until the render session shows up
        from pycaw.pycaw import AudioUtilities
        hits = []
        for _ in range(40):
            hits = find_pid(AudioUtilities.GetAllSessions(), tone_pid)
            if hits:
                break
            time.sleep(0.25)
        print(f"\n[SESSION] found {len(hits)} session(s) for PID {tone_pid}")
        for s in hits:
            print(f"  PID={s.ProcessId} State={s.State} DisplayName={s.DisplayName!r}")
        record(
            "4a. pycaw enumerates target render session",
            bool(hits),
            f"pid={tone_pid} sessions={len(hits)}",
        )
        enumerate_sessions("SESSIONS-PLAYBACK")

        # ---------------- Scenario A: main requested-format capture --------
        print("\n" + "-" * 78)
        print("SCENARIO A: capture with requested 48k/stereo/float32 on MTA thread")
        r1 = capture(tone_pid, args.duration, hns_buffer=0)
        print(f"  coinit_hr   = {hr_name(r1.info.get('coinit_hr', 0))}")
        print(f"  apartment   = {r1.info.get('apartment')}")
        print(f"  device      = {r1.info.get('device')!r}")
        print(f"  handler ifaces = {r1.info.get('handler_interfaces')}")
        print(f"  activate_async_hr = {hr_name(r1.info.get('activate_async_hr', 0))}")
        print(f"  get_activate_result_hr = {hr_name(r1.info.get('get_activate_result_hr', 0))}")
        print(f"  init hns_buffer used = {r1.info.get('hns_buffer_used')}")
        print(f"  requested_format = {r1.info.get('requested_format')}")
        print(f"  GetMixFormat     = {r1.info.get('mix_format')}")
        if r1.exception:
            print(f"  EXCEPTION   = {r1.exception}")
        print(f"  packets={r1.packets} frames={r1.frames} bytes={r1.bytes} "
              f"silent_packets={r1.silent_packets} silent_frames={r1.silent_frames}")
        print(f"  flags_histogram={ {hex(k): v for k, v in r1.flag_histogram.items()} }")
        print(f"  rms={r1.rms:.5f} peak={r1.peak:.5f}")
        expected = int(args.duration * REQUESTED_RATE * REQUESTED_CHANNELS * (REQUESTED_BITS // 8))
        ratio = r1.bytes / expected if expected else 0.0
        print(f"  expected_bytes~{expected} ratio={ratio:.3f}")
        record("a. literal VAD\\Process_Loopback activation", r1.ok,
               f"hr={hr_name(r1.hr)} device={VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK!r}")
        record("b. handler implements IAgileObject + completion",
               "IAgileObject" in (r1.info.get("handler_interfaces") or []),
               f"{r1.info.get('handler_interfaces')}")
        record("c. capture thread apartment = MTA",
               "MTA" in str(r1.info.get("apartment")),
               f"coinit={hr_name(r1.info.get('coinit_hr', 0))} apt={r1.info.get('apartment')}")
        record("d. buffers in requested 48k/2ch/f32 format",
               r1.ok and 0.7 <= ratio <= 1.4,
               f"ratio={ratio:.3f} (expected≈{expected} bytes)")
        record("d2. captured audio is non-silent tone (rms>0.02)",
               r1.rms > 0.02, f"rms={r1.rms:.4f} peak={r1.peak:.4f}")
        if r1.raw:
            dump_wav(args.out, bytes(r1.raw))
        main_act = getattr(r1, "_act", None)
        if main_act:
            r1.release()
            main_act = None

        # ---------------- Scenario F+: re-activate same PID ----------------
        print("\n" + "-" * 78)
        print("SCENARIO F+: re-activate SAME PID after full release (incl. async op)")
        r2 = capture(tone_pid, 2.0, hns_buffer=0)
        print(f"  activate_async_hr = {hr_name(r2.info.get('activate_async_hr', 0))}")
        print(f"  get_activate_result_hr = {hr_name(r2.info.get('get_activate_result_hr', 0))}")
        print(f"  packets={r2.packets} frames={r2.frames} bytes={r2.bytes} rms={r2.rms:.4f}")
        if r2.exception:
            print(f"  EXCEPTION = {r2.exception}")
        record("f+. re-activation of same PID succeeds after release", r2.ok,
               f"hr={hr_name(r2.hr)} packets={r2.packets}")
        if getattr(r2, "_act", None):
            r2.release()

        # ---------------- SILENT flag observation --------------------------
        print("\n" + "-" * 78)
        print("SCENARIO E: observe AUDCLNT_BUFFERFLAGS_SILENT during a tone gap")
        got_gap = player.wait_next_gap(timeout=30)
        print(f"  next tone gap observed = {got_gap}")
        r3 = capture(tone_pid, 4.0, hns_buffer=0)
        print(f"  packets={r3.packets} silent_packets={r3.silent_packets} "
              f"silent_frames={r3.silent_frames} rms={r3.rms:.5f} peak={r3.peak:.5f}")
        print(f"  flags_histogram={ {hex(k): v for k, v in r3.flag_histogram.items()} }")
        record("e (digest claim). SILENT (0x2) flag observable during a tone gap",
               r3.silent_packets > 0,
               f"silent_packets={r3.silent_packets} of {r3.packets} "
               f"(FAIL here == flag NOT observed; code path still implemented)")
        if r3.silent_packets == 0:
            print("  NOTE: no SILENT-flagged packets in this window; zeros were "
                  "delivered as real samples (rms reflects partial tone).")
        print("  (code path: flags & 0x2 -> zero-filled buffer, exercised by unit "
              "tests regardless)")
        if getattr(r3, "_act", None):
            r3.release()

        # ---------------- Negative: GUID device strings --------------------
        print("\n" + "-" * 78)
        print("NEGATIVE: activation with GUID strings instead of the literal")
        guid_variants = [
            ("IID_IAudioClient", "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"),
            ("endpoint-id style", "{0.0.0.00000000}.{69f8a1f7-7174-4221-9a2a-4220c044a603}"),
            ("bare GUID", "94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"),
        ]
        guid_hrs = {}
        for label, dev in guid_variants:
            r_guid = capture(tone_pid, 0.2, device=dev, hns_buffer=0)
            result_hr = r_guid.info.get("get_activate_result_hr", r_guid.hr)
            if result_hr == 0:
                result_hr = r_guid.hr
            guid_hrs[label] = result_hr
            print(f"  {label:18} device={dev!r} -> "
                  f"activate_async={hr_name(r_guid.info.get('activate_async_hr', 0))} "
                  f"result={hr_name(result_hr)}")
            if getattr(r_guid, "_act", None):
                r_guid.release()
        record("a-neg. GUID device strings rejected",
               all(v != 0 for v in guid_hrs.values()),
               "; ".join(f"{k}={hr_hex(v)}" for k, v in guid_hrs.items()))
        print("  NOTE: digest predicted 0x8000000E (E_ILLEGAL_METHOD_CALL) for a "
              "GUID string; observed HRESULTs above are the live truth.")

        # ---------------- Negative: handler without IAgileObject -----------
        print("\n" + "-" * 78)
        print("NEGATIVE: completion handler WITHOUT IAgileObject")
        r_noagile = capture(tone_pid, 0.3, hns_buffer=0, handler_cls=_NonAgileHandler)
        print(f"  activate_async_hr = {hr_name(r_noagile.info.get('activate_async_hr', 0))}")
        print(f"  get_activate_result_hr = {hr_name(r_noagile.info.get('get_activate_result_hr', 0))}")
        print(f"  query_interface_hr = {hr_name(r_noagile.info.get('query_interface_hr', 0))}")
        print(f"  init hr keys = "
              f"{ {k: hr_hex(v) for k, v in r_noagile.info.items() if k.startswith('init_hr')} }")
        print(f"  hr={hr_name(r_noagile.hr)} exception={r_noagile.exception}")
        observed = r_noagile.info.get("activate_async_hr", r_noagile.hr)
        record("b-neg. missing IAgileObject -> E_ILLEGAL_METHOD_CALL",
               observed == E_ILLEGAL_METHOD_CALL,
               f"hr={hr_name(observed)}")
        if getattr(r_noagile, "_act", None):
            r_noagile.release()

        # ---------------- Negative: STA thread -----------------------------
        print("\n" + "-" * 78)
        print("NEGATIVE: activation on STA thread (COINIT_APARTMENTTHREADED)")
        r_sta = capture(
            tone_pid, 1.0, do_coinit=True, coinit_mode=COINIT_APARTMENTTHREADED,
            hns_buffer=0,
        )
        print(f"  coinit_hr = {hr_name(r_sta.info.get('coinit_hr', 0))}")
        print(f"  apartment = {r_sta.info.get('apartment')}")
        print(f"  hr={hr_name(r_sta.hr)} ok={r_sta.ok} exception={r_sta.exception}")
        print(f"  packets={r_sta.packets} rms={r_sta.rms:.5f}")
        record("c-neg (digest claim). STA apartment FAILS",
               not r_sta.ok or r_sta.hr != 0,
               f"ok={r_sta.ok} hr={hr_name(r_sta.hr)} apt={r_sta.info.get('apartment')} "
               f"(FAIL here == digest claim NOT reproduced)")
        if getattr(r_sta, "_act", None):
            r_sta.release()

        # ---------------- Regression: no Release of async op ---------------
        print("\n" + "-" * 78)
        print("REGRESSION: activate, then DO NOT release async operation, "
              "then re-activate SAME PID")
        leak = capture(tone_pid, 0.5, hns_buffer=0)
        print(f"  leak capture ok={leak.ok} hr={hr_name(leak.hr)} "
              f"activate_async_hr={hr_name(leak.info.get('activate_async_hr', 0))}")
        leak_act = getattr(leak, "_act", None)
        if leak_act is not None:
            # Keep the async operation (and friends) alive by an explicit
            # AddRef, then drop all Python references. This is a stronger
            # simulation of "not releasing" than merely holding the object.
            for obj in (leak_act.operation, leak_act.activated,
                        leak_act.audio_client, leak_act.capture_client):
                if obj is not None:
                    try:
                        obj.AddRef()
                        LEAKED_OPERATIONS.append(obj)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  AddRef failed: {exc!r}")
            leak_act.release(keep_operation=True)
            print(f"  leaked refs held alive: {len(LEAKED_OPERATIONS)} "
                  f"(async op + activated + clients, AddRef'd)")
        r_re = capture(tone_pid, 0.5, hns_buffer=0)
        print(f"  re-activate: activate_async_hr="
              f"{hr_name(r_re.info.get('activate_async_hr', 0))} "
              f"get_activate_result_hr="
              f"{hr_name(r_re.info.get('get_activate_result_hr', 0))} "
              f"hr={hr_name(r_re.hr)} ok={r_re.ok} exception={r_re.exception}")
        record("f-reg (digest claim). re-activation WITHOUT async-op release FAILS",
               not r_re.ok,
               f"hr={hr_name(r_re.hr)} exc={r_re.exception} "
               f"(FAIL here == digest claim NOT reproduced)")
        if getattr(r_re, "_act", None):
            r_re.release()

        # ---------------- D6: target exits mid-capture ---------------------
        print("\n" + "-" * 78)
        print("SCENARIO D6: kill target 1.5s into a 5s capture; does the stream "
              "keep delivering silence?")
        r_exit = capture(tone_pid, 5.0, hns_buffer=0, kill_after=1.5,
                         kill_fn=player.kill)
        print(f"  ok={r_exit.ok} target_exited_mid={r_exit.target_exited_mid} "
              f"packets={r_exit.packets} frames={r_exit.frames} "
              f"bytes={r_exit.bytes} rms={r_exit.rms:.5f} peak={r_exit.peak:.5f}")
        print(f"  silent_packets={r_exit.silent_packets} "
              f"flags_histogram={ {hex(k): v for k, v in r_exit.flag_histogram.items()} }")
        record("D6. stream survives target exit (keeps delivering packets)",
               r_exit.ok and r_exit.target_exited_mid and r_exit.packets > 0,
               f"packets={r_exit.packets} rms_after_kill≈{r_exit.rms:.4f}")
        record("e2 (digest claim). SILENT (0x2) flag observable after target exit",
               r_exit.silent_packets > 0,
               f"silent_packets={r_exit.silent_packets} of {r_exit.packets} "
               f"(FAIL here == flag NOT observed)")
        if getattr(r_exit, "_act", None):
            r_exit.release()

    finally:
        player.kill()

    print("\n" + "=" * 78)
    print("SUMMARY")
    print("=" * 78)
    for item, status, note in REPORT:
        print(f"  [{status}] {item}  {note}")
    print("=" * 78)


if __name__ == "__main__":
    sys.exit(main())
