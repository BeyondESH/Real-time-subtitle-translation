"""
按进程音频捕获模块 - Windows WASAPI Process Loopback（Windows 10 2004+）

本模块是**传输层**：只负责把目标进程树的音频以
``48kHz / stereo / float32`` 的原始帧回调出去，不做单声道化/重采样——
集成方（AudioCapture）复用既有 ``_process_audio(audio, 48000)`` 管线。

能力边界（实机校准，证据见 ``scripts/spike_process_loopback_report.md``）：
- 激活设备串必须是字面量 ``"VAD\\Process_Loopback"``（GUID 形式会被拒）；
- 完成回调对象必须同时实现 ``IActivateAudioInterfaceCompletionHandler``
  与 ``IAgileObject``（缺后者 → ``E_ILLEGAL_METHOD_CALL 0x8000000E``）；
- ``ActivateAudioInterfaceAsync`` 对非法参数也会先返回 ``S_OK``，
  真正的错误在 ``GetActivateResult`` 的结果 HRESULT 上——必须检查；
- 交付格式 = 请求格式（``GetMixFormat`` 在进程回环客户端返回 ``E_NOTIMPL``，不可用）；
- ``AUDCLNT_BUFFERFLAGS_SILENT`` 仅做**防御性**处理：实机静音以真实零样本、
  ``flags=0`` 交付（含目标进程退出后），静音判定交给 VAD。

不依赖具体音频硬件；模块导入不触发 COM 激活。
"""
from __future__ import annotations

import ctypes
import gc
import logging
import sys
import threading
from ctypes import (
    POINTER,
    byref,
    c_long,
    c_longlong,
    c_uint32,
    c_uint64,
    c_ushort,
    c_void_p,
    c_wchar_p,
)
from typing import Callable, Dict, Iterable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 公开常量
# --------------------------------------------------------------------------- #

#: 支持按进程捕获的最低 Windows Build（Windows 10 2004）
MIN_WINDOWS_BUILD = 19041

#: 请求格式：48kHz / 立体声 / float32（Integrated 层按此格式交付，绝不按 GetMixFormat 解读）
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 4
FRAME_BYTES = CHANNELS * BYTES_PER_SAMPLE

#: 进程回环虚拟设备**字面量**路径（GUID 字符串会被拒绝）
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

#: 未知进程名占位模板
UNKNOWN_PROCESS_NAME = "未知进程 ({pid})"

#: 音频数据回调：同步函数，参数为 float32 立体声 (frames, 2)、48kHz 原始帧
AudioFrameCallback = Callable[[np.ndarray], None]

# 激活类型 / 回环模式（audioclientactivationparams.h）
_AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
_VT_BLOB = 0x41

# 流标志 / 共享模式（audioclient.h）
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
_AUDCLNT_SHAREMODE_SHARED = 0
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2

# COM 线程模型 / 等待返回码
_COINIT_MULTITHREADED = 0x0
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102

# 激活等待与读循环参数
_ACTIVATION_WAIT_MS = 5000
_WAIT_TIMEOUT_MS = 200
_START_TIMEOUT_S = 6.0
_STOP_TIMEOUT_S = 2.0
#: Initialize 缓冲时长为 0（实证首跑即通过）；仅在 COM 报错时回退此值
_FALLBACK_BUFFER_DURATION = 2_000_000

# 会话状态（AudioSessionState: 0=Inactive, 1=Active, 2=Expired）
_SESSION_STATE_ACTIVE = 1


class ProcessLoopbackError(RuntimeError):
    """按进程回环捕获失败，携带 HRESULT 与细节供上层判定/降级。"""

    def __init__(self, message: str, hresult: Optional[int] = None,
                 detail: Optional[str] = None):
        super().__init__(message)
        self.hresult = hresult
        self.detail = detail


# --------------------------------------------------------------------------- #
# ctypes 结构与常量（纯 ctypes，可安全导入）
# --------------------------------------------------------------------------- #

class _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [
        ("TargetProcessId", c_uint32),
        ("ProcessLoopbackMode", c_uint32),
    ]


class _AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", c_uint32),
        ("ProcessLoopbackParams", _AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", c_ushort),
        ("nChannels", c_ushort),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_ushort),
        ("wBitsPerSample", c_ushort),
        ("cbSize", c_ushort),
    ]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_uint32), ("pBlobData", c_void_p)]


class _PROPVARIANT(ctypes.Structure):
    """携带 VT_BLOB 的 PROPVARIANT（64 位下 24 字节）。"""

    _fields_ = [
        ("vt", c_ushort),
        ("wReserved1", c_ushort),
        ("wReserved2", c_ushort),
        ("wReserved3", c_ushort),
        ("blob", _BLOB),
    ]


def _make_format_ieee_float(
    rate: int = SAMPLE_RATE, channels: int = CHANNELS, bits: int = 32
) -> _WAVEFORMATEX:
    """构造 IEEE float PCM 的 WAVEFORMATEX。"""
    fmt = _WAVEFORMATEX()
    fmt.wFormatTag = 0x0003  # WAVE_FORMAT_IEEE_FLOAT
    fmt.nChannels = channels
    fmt.nSamplesPerSec = rate
    fmt.wBitsPerSample = bits
    fmt.nBlockAlign = (channels * bits) // 8
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


def _build_activation_params(pid: int) -> _AUDIOCLIENT_ACTIVATION_PARAMS:
    """构造进程回环激活参数（INCLUDE 目标进程树）。"""
    params = _AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = _AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = int(pid)
    params.ProcessLoopbackParams.ProcessLoopbackMode = (
        _PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
    )
    return params


# HRESULT 名称映射（仅保留本模块可能遇到的常见值）
_HRESULT_NAMES = {
    0x00000000: "S_OK",
    0x00000001: "S_FALSE",
    0x8000000E: "E_ILLEGAL_METHOD_CALL",
    0x80004001: "E_NOTIMPL",
    0x80004003: "E_POINTER",
    0x80004005: "E_FAIL",
    0x80004002: "E_NOINTERFACE",
    0x80070005: "E_ACCESSDENIED",
    0x80070057: "E_INVALIDARG",
    0x80070490: "ERROR_NOT_FOUND",
    0x80010106: "RPC_E_CHANGED_MODE",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED",
    0x88890009: "AUDCLNT_E_UNSUPPORTED_FORMAT",
    0x8889000B: "AUDCLNT_E_DEVICE_IN_USE",
    0x88890021: "AUDCLNT_E_RESOURCES_INVALIDATED",
}


def _hresult_name(hr: int) -> str:
    """把 HRESULT 格式化为 ``名称 (0xXXXXXXXX)``。"""
    value = hr & 0xFFFFFFFF
    return f"{_HRESULT_NAMES.get(value, 'UNKNOWN')} (0x{value:08X})"


# --------------------------------------------------------------------------- #
# COM 定义（comtypes 仅在可用时定义；导入本模块不触发 COM 激活）
# --------------------------------------------------------------------------- #

# comtypes / pycaw 延迟导入：``import process_loopback`` 不触发任何 COM 初始化。
# comtypes 在**导入时**即按 ``sys.coinit_flags`` 初始化调用线程；本项目 soundcard
# 会把线程初始化为 MTA（实测 apartment=1）。若在导入阶段按默认 STA 初始化，会与
# soundcard 冲突（RPC_E_CHANGED_MODE，WinError -2147417850）；但若抢先按 MTA 初始化，
# 后导入的 soundcard 又会因 CoInitializeEx(MTA) 返回 S_FALSE 而报错。
# 延迟到首次使用再导入，即可对两种导入顺序都安全。
comtypes = None  # type: ignore[assignment]
_COMTYPES_AVAILABLE = False
_COMTYPES_IMPORT_ERROR: Optional[BaseException] = None
_com_lock = threading.Lock()

#: REFERENCE_TIME = LONGLONG（100ns 单位，x64 上必须 64 位；c_long 在 Windows 仅 32 位）
_REFERENCE_TIME = c_longlong

# COM 层符号，首次使用时由 _ensure_com() 填充
IUnknown = None
_IID_ACTIVATE_COMPLETION_HANDLER = None
_IID_AGILE_OBJECT = None
_IID_AUDIO_CLIENT = None
_IID_AUDIO_CAPTURE_CLIENT = None
_IActivateAudioInterfaceCompletionHandler = None
_IAgileObject = None
_IAudioClient = None
_IAudioCaptureClient = None
_CompletionHandler = None


def _ensure_com() -> bool:
    """首次使用时导入 comtypes 并构建 COM 层；成功返回 True。

    在导入 comtypes 前把 ``sys.coinit_flags`` 统一为 MTA（若尚未设置），
    使会话枚举（主线程已被 soundcard 置为 MTA）与捕获线程同公寓可共存。
    """
    global comtypes, IUnknown, _COMTYPES_AVAILABLE, _COMTYPES_IMPORT_ERROR
    global _IID_ACTIVATE_COMPLETION_HANDLER, _IID_AGILE_OBJECT
    global _IID_AUDIO_CLIENT, _IID_AUDIO_CAPTURE_CLIENT
    global _IActivateAudioInterfaceCompletionHandler, _IAgileObject
    global _IAudioClient, _IAudioCaptureClient, _CompletionHandler

    if _COMTYPES_AVAILABLE:
        return True
    with _com_lock:
        if _COMTYPES_AVAILABLE:
            return True
        if not hasattr(sys, "coinit_flags"):
            sys.coinit_flags = _COINIT_MULTITHREADED
        try:
            import comtypes
            from comtypes import COMMETHOD, GUID
        except Exception as exc:  # noqa: BLE001 - 非 Windows / 依赖缺失时静默降级
            _COMTYPES_IMPORT_ERROR = exc
            return False
        IUnknown = comtypes.IUnknown  # 供类定义与激活函数全局引用

        _IID_ACTIVATE_COMPLETION_HANDLER = GUID(
            "{41D949AB-9862-444A-80F6-C261334DA5EB}"
        )
        _IID_AGILE_OBJECT = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        _IID_AUDIO_CLIENT = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
        _IID_AUDIO_CAPTURE_CLIENT = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")

        class _IActivateAudioInterfaceCompletionHandler(IUnknown):
            _iid_ = _IID_ACTIVATE_COMPLETION_HANDLER
            _methods_ = [
                COMMETHOD(
                    [], comtypes.HRESULT, "ActivateCompleted",
                    (["in"], c_void_p, "activateOperation"),
                ),
            ]

        class _IAgileObject(IUnknown):
            """公寓中立标记接口；完成回调对象缺此接口会被 COM 拒绝。"""

            _iid_ = _IID_AGILE_OBJECT
            _methods_ = []

        class _IAudioClient(IUnknown):
            _iid_ = _IID_AUDIO_CLIENT
            _methods_ = [
                COMMETHOD([], comtypes.HRESULT, "Initialize",
                          (["in"], c_uint32, "ShareMode"),
                          (["in"], c_uint32, "StreamFlags"),
                          (["in"], _REFERENCE_TIME, "hnsBufferDuration"),
                          (["in"], _REFERENCE_TIME, "hnsPeriodicity"),
                          (["in"], POINTER(_WAVEFORMATEX), "pFormat"),
                          (["in"], POINTER(GUID), "AudioSessionGuid")),
                COMMETHOD([], comtypes.HRESULT, "GetBufferSize",
                          (["out"], POINTER(c_uint32), "pNumBufferFrames")),
                COMMETHOD([], comtypes.HRESULT, "GetStreamLatency",
                          (["out"], POINTER(_REFERENCE_TIME), "phnsLatency")),
                COMMETHOD([], comtypes.HRESULT, "GetCurrentPadding",
                          (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
                COMMETHOD([], comtypes.HRESULT, "IsFormatSupported",
                          (["in"], c_uint32, "ShareMode"),
                          (["in"], POINTER(_WAVEFORMATEX), "pFormat"),
                          (["out"], POINTER(POINTER(_WAVEFORMATEX)), "ppClosestMatch")),
                COMMETHOD([], comtypes.HRESULT, "GetMixFormat",
                          (["out"], POINTER(POINTER(_WAVEFORMATEX)), "ppDeviceFormat")),
                COMMETHOD([], comtypes.HRESULT, "GetDevicePeriod",
                          (["out"], POINTER(_REFERENCE_TIME), "phnsDefaultDevicePeriod"),
                          (["out"], POINTER(_REFERENCE_TIME), "phnsMinimumDevicePeriod")),
                COMMETHOD([], comtypes.HRESULT, "Start"),
                COMMETHOD([], comtypes.HRESULT, "Stop"),
                COMMETHOD([], comtypes.HRESULT, "Reset"),
                COMMETHOD([], comtypes.HRESULT, "SetEventHandle",
                          (["in"], c_void_p, "eventHandle")),
                COMMETHOD([], comtypes.HRESULT, "GetService",
                          (["in"], POINTER(GUID), "riid"),
                          (["out"], POINTER(c_void_p), "ppv")),
            ]

        class _IAudioCaptureClient(IUnknown):
            _iid_ = _IID_AUDIO_CAPTURE_CLIENT
            _methods_ = [
                COMMETHOD([], comtypes.HRESULT, "GetBuffer",
                          (["out"], POINTER(POINTER(ctypes.c_ubyte)), "ppData"),
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
            """激活完成回调：同时暴露 completion + agile 两个接口。

            缺 ``IAgileObject`` 时 ``ActivateAudioInterfaceAsync`` 会同步返回
            ``E_ILLEGAL_METHOD_CALL (0x8000000E)``。
            """

            _com_interfaces_ = [
                _IActivateAudioInterfaceCompletionHandler,
                _IAgileObject,
            ]

            def __init__(self, event_handle):
                super().__init__()
                self._event = event_handle

            def ActivateCompleted(self, activate_operation):
                ctypes.windll.kernel32.SetEvent(self._event)
                return 0

        _COMTYPES_AVAILABLE = True
        return True


# --------------------------------------------------------------------------- #
# 底层 COM 辅助（raw vtable：用于显式释放 async operation / 读取激活结果）
# --------------------------------------------------------------------------- #

def _call_get_activate_result(operation_addr: int):
    """调用 ``IActivateAudioInterfaceAsyncOperation::GetActivateResult``。

    返回 ``(activate_hr, activated_addr)``；调用本身的 HRESULT 非 0 时抛错。
    """
    try:
        vtable = ctypes.cast(
            c_void_p(operation_addr), POINTER(POINTER(c_void_p))
        )[0]
        prototype = ctypes.WINFUNCTYPE(
            c_long, c_void_p, POINTER(c_long), POINTER(c_void_p)
        )
        get_result = prototype(vtable[3])
        activate_hr = c_long(0)
        activated = c_void_p(0)
        call_hr = get_result(
            c_void_p(operation_addr), byref(activate_hr), byref(activated)
        )
    except OSError as exc:  # pragma: no cover - 仅真实 COM 环境
        raise ProcessLoopbackError("GetActivateResult 调用失败", detail=repr(exc)) from exc

    if call_hr != 0:
        hr = call_hr & 0xFFFFFFFF
        raise ProcessLoopbackError(
            f"GetActivateResult 调用失败: {_hresult_name(hr)}", hresult=hr
        )
    return activate_hr.value & 0xFFFFFFFF, (activated.value or 0)


def _release_com_pointer(addr: int) -> None:
    """对原始 COM 接口指针调用一次 ``Release()``（用于 async operation）。"""
    if not addr:
        return
    try:
        vtable = ctypes.cast(
            c_void_p(addr), POINTER(POINTER(c_void_p))
        )[0]
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[2])
        release(c_void_p(addr))
    except Exception:  # noqa: BLE001 - 释放失败不应影响主流程
        logger.debug("释放 COM 指针失败 addr=0x%X", addr, exc_info=True)


def _resolve_activation(operation_addr: int) -> int:
    """检查 ``GetActivateResult`` 的 HRESULT，返回 IAudioClient 原始指针地址。

    ``ActivateAudioInterfaceAsync`` 本身返回 ``S_OK`` 不代表成功（例如 GUID
    形式的设备串），必须以本结果为真值。
    """
    activate_hr, activated_addr = _call_get_activate_result(operation_addr)
    if activate_hr != 0 or not activated_addr:
        raise ProcessLoopbackError(
            f"GetActivateResult 返回失败: {_hresult_name(activate_hr)}",
            hresult=activate_hr or 0x80004003,
        )
    return activated_addr


# --------------------------------------------------------------------------- #
# 采集激活对象（COM 资源生命周期）
# --------------------------------------------------------------------------- #

class _Activation:
    """一次成功激活持有的全部 COM 资源，``release()`` 做确定性清理。"""

    __slots__ = (
        "audio_client", "capture_client", "activated", "operation_addr",
        "handler", "handler_ptr", "capture_event", "completion_event",
    )

    def __init__(self):
        self.audio_client = None
        self.capture_client = None
        self.activated = None
        self.operation_addr: Optional[int] = None
        self.handler = None
        self.handler_ptr = None
        self.capture_event = None
        self.completion_event = None

    def release(self) -> None:
        """Stop 流、关闭事件句柄、显式 Release async operation、释放接口。"""
        if self.audio_client is not None:
            try:
                self.audio_client.Stop()
            except Exception:  # noqa: BLE001 - 流可能未成功启动
                logger.debug("IAudioClient.Stop 失败（忽略）", exc_info=True)

        for name in ("capture_event", "completion_event"):
            handle = getattr(self, name)
            if handle:
                try:
                    ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    logger.debug("CloseHandle(%s) 失败（忽略）", name, exc_info=True)
                setattr(self, name, None)

        if self.operation_addr:
            _release_com_pointer(self.operation_addr)
            self.operation_addr = None

        # comtypes 指针在 __del__ 中 Release；置空 + gc 触发即时回收
        self.audio_client = None
        self.capture_client = None
        self.activated = None
        self.handler = None
        self.handler_ptr = None
        gc.collect()


def _co_initialize_mta() -> int:
    """在调用线程执行 ``CoInitializeEx(MTA)``，返回 HRESULT。"""
    return ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)


def _wait_for_event(event_handle, timeout_ms: int) -> int:
    """``WaitForSingleObject`` 薄封装（测试可替换）。"""
    return ctypes.windll.kernel32.WaitForSingleObject(event_handle, timeout_ms)


def _decode_packet(data_ptr, frames: int, flags: int) -> np.ndarray:
    """把 WASAPI 包转为 float32 立体声帧 ``(frames, 2)``；SILENT 补零。"""
    if frames <= 0:
        return np.empty((0, CHANNELS), dtype=np.float32)
    if flags & _AUDCLNT_BUFFERFLAGS_SILENT:
        return np.zeros((frames, CHANNELS), dtype=np.float32)
    raw = ctypes.string_at(data_ptr, frames * FRAME_BYTES)
    return np.frombuffer(raw, dtype=np.float32).reshape(-1, CHANNELS).copy()


def _activate_process_loopback(pid: int) -> _Activation:
    """激活指定 PID（含目标进程树）的进程回环并启动读取。

    必须在 MTA 线程中调用；失败抛 :class:`ProcessLoopbackError`。
    """
    if not _ensure_com():
        raise ProcessLoopbackError(
            "comtypes 不可用，无法进行按进程捕获",
            detail=repr(_COMTYPES_IMPORT_ERROR),
        )

    activation = _Activation()
    try:
        params = _build_activation_params(pid)
        propvariant = _PROPVARIANT()
        propvariant.vt = _VT_BLOB
        propvariant.blob.cbSize = ctypes.sizeof(params)
        propvariant.blob.pBlobData = ctypes.cast(
            ctypes.addressof(params), c_void_p
        )

        completion_event = ctypes.windll.kernel32.CreateEventW(None, True, False, None)
        if not completion_event:
            raise ProcessLoopbackError("CreateEventW(完成事件) 失败")
        activation.completion_event = completion_event

        handler = _CompletionHandler(completion_event)
        activation.handler = handler
        handler_ptr = handler.QueryInterface(_IActivateAudioInterfaceCompletionHandler)
        activation.handler_ptr = handler_ptr
        handler_raw = ctypes.cast(handler_ptr, c_void_p).value

        mmdev = ctypes.WinDLL("Mmdevapi.dll")
        activate_async = mmdev.ActivateAudioInterfaceAsync
        activate_async.restype = c_long
        activate_async.argtypes = [c_wchar_p, c_void_p, c_void_p, c_void_p, c_void_p]

        operation_holder = c_void_p(0)
        iid_addr = ctypes.cast(ctypes.pointer(_IID_AUDIO_CLIENT), c_void_p).value
        pv_addr = ctypes.cast(ctypes.pointer(propvariant), c_void_p).value
        call_hr = activate_async(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            iid_addr,
            pv_addr,
            handler_raw,
            ctypes.cast(ctypes.pointer(operation_holder), c_void_p).value,
        )
        if call_hr != 0:
            hr = call_hr & 0xFFFFFFFF
            raise ProcessLoopbackError(
                f"ActivateAudioInterfaceAsync 失败: {_hresult_name(hr)}",
                hresult=hr,
            )
        activation.operation_addr = operation_holder.value
        if not activation.operation_addr:
            raise ProcessLoopbackError("ActivateAudioInterfaceAsync 未返回操作对象")

        wait = ctypes.windll.kernel32.WaitForSingleObject(
            completion_event, _ACTIVATION_WAIT_MS
        )
        if wait != _WAIT_OBJECT_0:
            raise ProcessLoopbackError(
                f"等待激活完成失败: 0x{wait & 0xFFFFFFFF:08X}",
                detail=f"pid={pid}",
            )
        ctypes.windll.kernel32.CloseHandle(completion_event)
        activation.completion_event = None

        # 关键：以 GetActivateResult 的 HRESULT 为真值（async 调用本身返回 S_OK 不算数）
        activated_addr = _resolve_activation(activation.operation_addr)
        activated = ctypes.cast(c_void_p(activated_addr), POINTER(IUnknown))
        activation.activated = activated
        audio_client = activated.QueryInterface(_IAudioClient)
        activation.audio_client = audio_client

        fmt = _make_format_ieee_float()
        flags = _AUDCLNT_STREAMFLAGS_LOOPBACK | _AUDCLNT_STREAMFLAGS_EVENTCALLBACK
        last_hr = 0
        for buffer_duration in (0, _FALLBACK_BUFFER_DURATION):
            try:
                audio_client.Initialize(
                    _AUDCLNT_SHAREMODE_SHARED, flags, buffer_duration, 0,
                    byref(fmt), None,
                )
                last_hr = 0
                break
            except comtypes.COMError as exc:  # type: ignore[union-attr]
                last_hr = exc.hresult & 0xFFFFFFFF
                logger.debug(
                    "Initialize 失败（buffer_duration=%s）: %s",
                    buffer_duration, _hresult_name(last_hr),
                )
        if last_hr != 0:
            raise ProcessLoopbackError(
                f"IAudioClient.Initialize 失败: {_hresult_name(last_hr)}",
                hresult=last_hr,
            )

        capture_void = audio_client.GetService(byref(_IID_AUDIO_CAPTURE_CLIENT))
        activation.capture_client = ctypes.cast(
            capture_void, POINTER(_IAudioCaptureClient)
        )

        capture_event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
        if not capture_event:
            raise ProcessLoopbackError("CreateEventW(捕获事件) 失败")
        activation.capture_event = capture_event
        audio_client.SetEventHandle(capture_event)
        audio_client.Start()
        return activation

    except ProcessLoopbackError:
        activation.release()
        raise
    except Exception as exc:  # noqa: BLE001 - comtypes.COMError 等统一包装
        hr = getattr(exc, "hresult", None)
        hr = (hr & 0xFFFFFFFF) if hr is not None else None
        activation.release()
        if hr is not None:
            raise ProcessLoopbackError(
                f"进程回环 COM 调用失败: {_hresult_name(hr)}",
                hresult=hr,
            ) from exc
        raise ProcessLoopbackError(
            f"进程回环激活失败: {exc}", detail=repr(exc)
        ) from exc


# --------------------------------------------------------------------------- #
# 捕获类（传输层）
# --------------------------------------------------------------------------- #

class ProcessLoopbackCapture:
    """按进程 WASAPI 回环捕获。

    回调接收 ``48kHz / stereo / float32`` 的原始帧 ``(frames, 2)``；
    单声道化与 48k→16k 重采样由集成层复用 ``AudioCapture._process_audio`` 完成。
    """

    def __init__(self, pid: int, callback: AudioFrameCallback):
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"非法的进程 PID: {pid!r}")
        if not callable(callback):
            raise ValueError("callback 必须是可调用对象")
        self._pid = int(pid)
        self._callback = callback
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._ready = threading.Event()
        self._activation: Optional[_Activation] = None
        self._start_error: Optional[ProcessLoopbackError] = None
        self._running = False

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """启动捕获线程并等待激活结果；激活失败抛 :class:`ProcessLoopbackError`。"""
        if self._running:
            logger.warning("进程回环捕获已在运行 (pid=%s)", self._pid)
            return
        if not supported():
            raise ProcessLoopbackError(
                "当前系统不支持按进程音频捕获（需 Windows 10 2004 / Build "
                f"{MIN_WINDOWS_BUILD}+）"
            )

        self._stop_event.clear()
        self._ready.clear()
        self._start_error = None
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            name=f"process-loopback-{self._pid}",
            daemon=True,
        )
        self._thread.start()

        if not self._ready.wait(_START_TIMEOUT_S):
            self.stop()
            raise ProcessLoopbackError(
                "激活进程回环超时", detail=f"pid={self._pid}"
            )
        if self._start_error is not None:
            error = self._start_error
            self._start_error = None
            self.stop()
            raise error

    def stop(self, timeout: float = _STOP_TIMEOUT_S) -> None:
        """请求停止并等待捕获线程退出（幂等）。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None
        self._running = False

    def _run(self) -> None:
        activation: Optional[_Activation] = None
        coinit_hr: Optional[int] = None
        try:
            coinit_hr = _co_initialize_mta()
            activation = _activate_process_loopback(self._pid)
            self._activation = activation
            logger.info("进程回环捕获已启动 (pid=%s)", self._pid)
            self._ready.set()
            self._capture_loop(activation)
        except ProcessLoopbackError as exc:
            logger.error("进程回环捕获启动失败 (pid=%s): %s", self._pid, exc)
            self._start_error = exc
            self._ready.set()
        except Exception as exc:  # noqa: BLE001 - 捕获线程不得让异常逃逸
            logger.exception("进程回环捕获线程异常 (pid=%s)", self._pid)
            self._start_error = ProcessLoopbackError(
                f"进程回环捕获失败: {exc}", detail=repr(exc)
            )
            self._ready.set()
        finally:
            current = self._activation
            self._activation = None
            if current is not None:
                current.release()
            self._running = False
            # 仅当本线程首次成功初始化 MTA 时配对 CoUninitialize
            if coinit_hr == 0:
                try:
                    ctypes.windll.ole32.CoUninitialize()
                except Exception:  # noqa: BLE001
                    logger.debug("CoUninitialize 失败（忽略）", exc_info=True)

    def _capture_loop(self, activation: _Activation) -> None:
        """事件驱动读循环，直到 stop() 或流失效。"""
        while not self._stop_event.is_set():
            wait = _wait_for_event(activation.capture_event, _WAIT_TIMEOUT_MS)
            if wait == _WAIT_TIMEOUT:
                continue
            if wait != _WAIT_OBJECT_0:
                logger.warning(
                    "等待捕获事件返回 0x%08X，结束读循环 (pid=%s)",
                    wait & 0xFFFFFFFF, self._pid,
                )
                break
            self._drain_packets(activation.capture_client)

    def _drain_packets(self, capture_client) -> int:
        """排空当前可用包并回调（返回本轮发出的包数）。"""
        emitted = 0
        while True:
            frames = capture_client.GetNextPacketSize()
            if not frames:
                return emitted
            data_ptr, num_frames, flags, _device_pos, _qpc_pos = capture_client.GetBuffer()
            try:
                audio = _decode_packet(data_ptr, int(num_frames), int(flags))
            finally:
                capture_client.ReleaseBuffer(num_frames)
            emitted += 1
            if self._callback is not None and audio.size:
                try:
                    self._callback(audio)
                except Exception:  # noqa: BLE001 - 回调异常不终止读循环
                    logger.exception("进程回环音频回调异常 (pid=%s)", self._pid)


# --------------------------------------------------------------------------- #
# 能力门控
# --------------------------------------------------------------------------- #

def _current_build() -> Optional[int]:
    """返回当前 Windows Build；非 Windows 或取不到返回 None。"""
    try:
        if sys.platform != "win32":
            return None
        return int(sys.getwindowsversion().build)
    except Exception:  # noqa: BLE001 - 任何平台/API 异常一律视为不支持
        return None


def supported() -> bool:
    """是否支持按进程捕获（Windows Build >= 19041）。"""
    try:
        build = _current_build()
        return build is not None and build >= MIN_WINDOWS_BUILD
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# 音频会话枚举（纯函数 + pycaw 薄封装）
# --------------------------------------------------------------------------- #

def _session_is_system_sounds(session) -> bool:
    """判定音频会话是否为系统声音（兼容 pycaw 不同版本 API）。

    pycaw 20251023 的高层 ``AudioSession`` 不暴露该方法，需回退到
    ``session._ctl.IsSystemSoundsSession()``；``S_OK``（0）即系统声音。
    """
    try:
        method = getattr(session, "IsSystemSoundsSession", None)
        if method is None:
            control = getattr(session, "_ctl", None)
            method = getattr(control, "IsSystemSoundsSession", None)
        if method is None:
            return False
        return method() == 0
    except Exception:  # noqa: BLE001 - 判定失败时按非系统声音处理
        return False


def _session_is_active(session) -> bool:
    """会话是否处于 Active 状态。"""
    try:
        return int(session.State) == _SESSION_STATE_ACTIVE
    except Exception:  # noqa: BLE001
        return False


def _resolve_process_name(pid: int) -> Optional[str]:
    """通过 psutil 解析进程名；失败返回 None（由纯函数填占位名）。"""
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 - 权限/进程已退出等
        return None


def build_process_entries(sessions: Iterable[Dict]) -> List[dict]:
    """把会话级原始数据整理为进程级列表（纯函数，便于单测）。

    Args:
        sessions: 可迭代的 session-like dict，字段
            ``{pid, name, active, is_system_sounds}``。

    Returns:
        每项 ``{pid, name, active, ordinal}``：系统声音/PID 0 已排除；
        同 PID 多会话合并（任一 Active 即 Active）；同名多进程按
        "活动优先、PID 升序"编号（唯一名 ``ordinal=None``）；组间
        "含活动成员优先、名称升序"。
    """
    merged: Dict[int, Dict] = {}
    for session in sessions:
        try:
            pid = int(session.get("pid"))
        except (TypeError, ValueError, AttributeError):
            continue
        if pid <= 0:
            continue
        if session.get("is_system_sounds"):
            continue
        name = session.get("name")
        name = str(name).strip() if name else ""
        active = bool(session.get("active"))

        entry = merged.get(pid)
        if entry is None:
            merged[pid] = {"pid": pid, "name": name, "active": active}
        else:
            entry["active"] = entry["active"] or active
            if not entry["name"] and name:
                entry["name"] = name

    for entry in merged.values():
        if not entry["name"]:
            entry["name"] = UNKNOWN_PROCESS_NAME.format(pid=entry["pid"])

    groups: Dict[str, List[Dict]] = {}
    for entry in merged.values():
        groups.setdefault(entry["name"], []).append(entry)

    ordered: List[dict] = []
    for _name, members in sorted(
        groups.items(),
        key=lambda item: (not any(m["active"] for m in item[1]), item[0]),
    ):
        members.sort(key=lambda m: (not m["active"], m["pid"]))
        is_group = len(members) > 1
        for index, member in enumerate(members, start=1):
            ordered.append({
                "pid": member["pid"],
                "name": member["name"],
                "active": member["active"],
                "ordinal": index if is_group else None,
            })
    return ordered


def list_audio_processes() -> List[dict]:
    """枚举默认渲染端点上的可捕获应用进程（口径=音量合成器）。

    不支持的系统返回空列表；枚举失败抛 :class:`ProcessLoopbackError`，
    供协议层转换为 ``enumerate_failed``。
    """
    if not supported():
        return []

    # pycaw 依赖 comtypes，导入时会初始化调用线程公寓；与 soundcard 的 MTA 对齐
    if not hasattr(sys, "coinit_flags"):
        sys.coinit_flags = _COINIT_MULTITHREADED
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as exc:  # noqa: BLE001
        raise ProcessLoopbackError(
            "无法加载 pycaw，音频会话枚举不可用", detail=repr(exc)
        ) from exc

    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception as exc:  # noqa: BLE001
        raise ProcessLoopbackError("音频会话枚举失败", detail=repr(exc)) from exc

    raw: List[Dict] = []
    for session in sessions:
        try:
            pid = int(session.ProcessId)
        except Exception:  # noqa: BLE001
            continue
        raw.append({
            "pid": pid,
            "name": _resolve_process_name(pid),
            "active": _session_is_active(session),
            "is_system_sounds": _session_is_system_sounds(session) or pid == 0,
        })
    return build_process_entries(raw)
