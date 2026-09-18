"""
按进程回环捕获模块测试（纯函数 + 假 COM 层）

不依赖真实音频设备、不做真实 COM 激活；实机证据见
``scripts/spike_process_loopback_report.md``。
"""
import sys
import types

import numpy as np
import pytest

import process_loopback as pl


def make_session(pid, name=None, active=False, is_system_sounds=False):
    return {
        "pid": pid,
        "name": name,
        "active": active,
        "is_system_sounds": is_system_sounds,
    }


class _FakeCaptureClient:
    """假 IAudioCaptureClient：按包序列返回 GetNextPacketSize/GetBuffer。"""

    def __init__(self, packets):
        # packets: list[(data_ptr, frames, flags)]
        self._packets = list(packets)
        self.released = []

    def GetNextPacketSize(self):
        return self._packets[0][1] if self._packets else 0

    def GetBuffer(self):
        data_ptr, frames, flags = self._packets[0]
        return data_ptr, frames, flags, 0, 0

    def ReleaseBuffer(self, frames):
        self.released.append(frames)
        self._packets.pop(0)


class _FakeActivation:
    def __init__(self, capture_client=None, capture_event=0):
        self.capture_client = capture_client or _FakeCaptureClient([])
        self.capture_event = capture_event
        self.released = False

    def release(self):
        self.released = True


class _FakePycawSession:
    def __init__(self, pid, state=0, system=None, use_ctl=False):
        self.ProcessId = pid
        self.State = state
        if use_ctl:
            self._ctl = types.SimpleNamespace(IsSystemSoundsSession=lambda: system)
        elif system is not None:
            self.IsSystemSoundsSession = lambda: system


# --------------------------------------------------------------------------- #
# 纯函数：会话 → 进程条目
# --------------------------------------------------------------------------- #

class TestBuildProcessEntries:
    def test_excludes_system_sounds_and_pid_zero(self):
        entries = pl.build_process_entries([
            make_session(0, "系统声音", is_system_sounds=True),
            make_session(10, "伪装系统声音", is_system_sounds=True),
            make_session(20, "app.exe", active=True),
        ])
        assert [e["pid"] for e in entries] == [20]

    def test_dedupe_by_pid_active_wins(self):
        entries = pl.build_process_entries([
            make_session(100, "chrome.exe", active=False),
            make_session(100, "chrome.exe", active=True),
        ])
        assert len(entries) == 1
        assert entries[0]["active"] is True
        assert entries[0]["ordinal"] is None

    def test_dedupe_prefers_real_name(self):
        entries = pl.build_process_entries([
            make_session(5, None, active=False),
            make_session(5, "vlc.exe", active=True),
        ])
        assert entries[0]["name"] == "vlc.exe"
        assert entries[0]["active"] is True

    def test_unique_name_ordinal_none(self):
        entries = pl.build_process_entries([
            make_session(1, "a.exe"),
            make_session(2, "b.exe"),
        ])
        assert [e["ordinal"] for e in entries] == [None, None]

    def test_same_name_ordinal_active_first_then_pid(self):
        entries = pl.build_process_entries([
            make_session(300, "chrome.exe", active=False),
            make_session(100, "chrome.exe", active=True),
            make_session(200, "chrome.exe", active=False),
        ])
        assert [(e["pid"], e["ordinal"]) for e in entries] == [
            (100, 1), (200, 2), (300, 3)
        ]
        assert entries[0]["active"] is True

    def test_group_order_active_first_then_name(self):
        entries = pl.build_process_entries([
            make_session(1, "zebra.exe", active=True),
            make_session(2, "alpha.exe", active=False),
            make_session(3, "mango.exe", active=False),
        ])
        assert [e["name"] for e in entries] == [
            "zebra.exe", "alpha.exe", "mango.exe"
        ]

    def test_group_order_inactive_by_name(self):
        entries = pl.build_process_entries([
            make_session(1, "zebra.exe", active=False),
            make_session(2, "alpha.exe", active=False),
        ])
        assert [e["name"] for e in entries] == ["alpha.exe", "zebra.exe"]

    def test_name_fallback(self):
        entries = pl.build_process_entries([
            make_session(123, None),
            make_session(456, ""),
        ])
        names = {e["pid"]: e["name"] for e in entries}
        assert names[123] == "未知进程 (123)"
        assert names[456] == "未知进程 (456)"

    def test_empty(self):
        assert pl.build_process_entries([]) == []

    def test_invalid_entries_ignored(self):
        assert pl.build_process_entries([{"pid": "x"}, None, {"pid": -5}]) == []


# --------------------------------------------------------------------------- #
# 能力门控
# --------------------------------------------------------------------------- #

class TestSupported:
    def test_build_gate(self, monkeypatch):
        monkeypatch.setattr(pl, "_current_build", lambda: pl.MIN_WINDOWS_BUILD)
        assert pl.supported() is True
        monkeypatch.setattr(pl, "_current_build", lambda: pl.MIN_WINDOWS_BUILD - 1)
        assert pl.supported() is False
        monkeypatch.setattr(pl, "_current_build", lambda: None)
        assert pl.supported() is False

    def test_supported_swallows_exceptions(self, monkeypatch):
        def boom():
            raise RuntimeError("x")
        monkeypatch.setattr(pl, "_current_build", boom)
        assert pl.supported() is False

    def test_current_build_non_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert pl._current_build() is None

    def test_current_build_exception(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")

        def boom():
            raise OSError("no windows")

        monkeypatch.setattr(sys, "getwindowsversion", boom)
        assert pl._current_build() is None


# --------------------------------------------------------------------------- #
# 激活参数 / GetActivateResult HRESULT 检查
# --------------------------------------------------------------------------- #

class TestActivationParams:
    def test_literal_device_string(self):
        assert pl.VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK == "VAD\\Process_Loopback"

    def test_build_activation_params(self):
        params = pl._build_activation_params(4321)
        assert params.ActivationType == 1
        assert params.ProcessLoopbackParams.TargetProcessId == 4321
        assert params.ProcessLoopbackParams.ProcessLoopbackMode == 0


class TestResolveActivation:
    def test_bad_hresult_raises(self, monkeypatch):
        monkeypatch.setattr(
            pl, "_call_get_activate_result", lambda addr: (0x80070057, 0)
        )
        with pytest.raises(pl.ProcessLoopbackError) as excinfo:
            pl._resolve_activation(0x1000)
        assert excinfo.value.hresult == 0x80070057
        assert "E_INVALIDARG" in str(excinfo.value)

    def test_missing_interface_raises(self, monkeypatch):
        monkeypatch.setattr(
            pl, "_call_get_activate_result", lambda addr: (0, None)
        )
        with pytest.raises(pl.ProcessLoopbackError) as excinfo:
            pl._resolve_activation(0x1000)
        assert excinfo.value.hresult == 0x80004003

    def test_success_returns_interface_address(self, monkeypatch):
        monkeypatch.setattr(
            pl, "_call_get_activate_result", lambda addr: (0, 0xABCD)
        )
        assert pl._resolve_activation(0x1000) == 0xABCD

    def test_release_null_pointer_is_noop(self):
        pl._release_com_pointer(0)  # 不应抛异常


# --------------------------------------------------------------------------- #
# 包解码：SILENT → 零；float32 立体声
# --------------------------------------------------------------------------- #

class TestDecodePacket:
    def test_silent_flag_zero_filled(self):
        audio = pl._decode_packet(None, 480, pl._AUDCLNT_BUFFERFLAGS_SILENT)
        assert audio.shape == (480, 2)
        assert audio.dtype == np.float32
        assert not audio.any()

    def test_passthrough_is_copied_float32_stereo(self):
        src = np.arange(480 * 2, dtype=np.float32).reshape(480, 2)
        audio = pl._decode_packet(src.ctypes.data, 480, 0)
        assert audio.shape == (480, 2)
        assert audio.dtype == np.float32
        assert np.array_equal(audio, src)
        audio[0, 0] = -1.0
        assert src[0, 0] == 0.0  # 回调方可安全持有独立副本

    def test_zero_frames(self):
        audio = pl._decode_packet(None, 0, 0)
        assert audio.shape == (0, 2)
        assert audio.dtype == np.float32


# --------------------------------------------------------------------------- #
# 假 COM 层：读包 → 回调
# --------------------------------------------------------------------------- #

class TestDrainPackets:
    def test_emits_float32_stereo_and_releases(self):
        src = np.linspace(-0.5, 0.5, 240 * 2, dtype=np.float32).reshape(240, 2)
        client = _FakeCaptureClient([(src.ctypes.data, 240, 0)])
        received = []
        capture = pl.ProcessLoopbackCapture(1234, received.append)

        assert capture._drain_packets(client) == 1
        assert client.released == [240]
        assert len(received) == 1
        assert received[0].shape == (240, 2)
        assert received[0].dtype == np.float32

    def test_silent_packet_emits_zeros(self):
        src = np.full((100, 2), 0.9, dtype=np.float32)
        client = _FakeCaptureClient(
            [(src.ctypes.data, 100, pl._AUDCLNT_BUFFERFLAGS_SILENT)]
        )
        received = []
        capture = pl.ProcessLoopbackCapture(1234, received.append)

        capture._drain_packets(client)
        assert not received[0].any()
        assert client.released == [100]

    def test_multiple_packets_order_preserved(self):
        first = np.full((10, 2), 1.0, dtype=np.float32)
        second = np.full((20, 2), 2.0, dtype=np.float32)
        client = _FakeCaptureClient([
            (first.ctypes.data, 10, 0),
            (second.ctypes.data, 20, 0),
        ])
        received = []
        capture = pl.ProcessLoopbackCapture(1234, received.append)

        assert capture._drain_packets(client) == 2
        assert [frame.shape[0] for frame in received] == [10, 20]
        assert client.released == [10, 20]

    def test_callback_exception_does_not_escape(self):
        src = np.zeros((10, 2), dtype=np.float32)
        client = _FakeCaptureClient([(src.ctypes.data, 10, 0)])

        def boom(_):
            raise RuntimeError("callback 炸了")

        capture = pl.ProcessLoopbackCapture(1234, boom)
        assert capture._drain_packets(client) == 1  # 异常被吞，不终止读循环


# --------------------------------------------------------------------------- #
# 生命周期：start/stop、激活失败、资源释放
# --------------------------------------------------------------------------- #

class TestCaptureLifecycle:
    def test_start_then_stop_releases_activation(self, monkeypatch):
        monkeypatch.setattr(pl, "supported", lambda: True)
        monkeypatch.setattr(pl, "_co_initialize_mta", lambda: 1)  # S_FALSE：跳过 CoUninitialize
        monkeypatch.setattr(pl, "_wait_for_event", lambda ev, ms: pl._WAIT_TIMEOUT)

        activation = _FakeActivation()
        seen = {}

        def fake_activate(pid):
            seen["pid"] = pid
            return activation

        monkeypatch.setattr(pl, "_activate_process_loopback", fake_activate)

        capture = pl.ProcessLoopbackCapture(4321, lambda frame: None)
        capture.start()
        assert capture.is_running is True
        assert seen["pid"] == 4321

        capture.stop()
        assert capture.is_running is False
        assert activation.released is True

    def test_activation_failure_raises_from_start(self, monkeypatch):
        monkeypatch.setattr(pl, "supported", lambda: True)
        monkeypatch.setattr(pl, "_co_initialize_mta", lambda: 1)

        def boom(pid):
            raise pl.ProcessLoopbackError("激活失败", hresult=0x80070057)

        monkeypatch.setattr(pl, "_activate_process_loopback", boom)

        capture = pl.ProcessLoopbackCapture(4321, lambda frame: None)
        with pytest.raises(pl.ProcessLoopbackError) as excinfo:
            capture.start()
        assert excinfo.value.hresult == 0x80070057
        assert capture.is_running is False

    def test_unsupported_start_raises(self, monkeypatch):
        monkeypatch.setattr(pl, "supported", lambda: False)
        capture = pl.ProcessLoopbackCapture(4321, lambda frame: None)
        with pytest.raises(pl.ProcessLoopbackError):
            capture.start()

    def test_invalid_arguments(self):
        with pytest.raises(ValueError):
            pl.ProcessLoopbackCapture(0, lambda frame: None)
        with pytest.raises(ValueError):
            pl.ProcessLoopbackCapture(1, None)


# --------------------------------------------------------------------------- #
# 会话判定兼容层 / pycaw 薄封装
# --------------------------------------------------------------------------- #

class TestSessionCompat:
    def test_system_sounds_high_level_method(self):
        assert pl._session_is_system_sounds(_FakePycawSession(1, system=0)) is True
        assert pl._session_is_system_sounds(_FakePycawSession(1, system=1)) is False

    def test_system_sounds_falls_back_to_ctl(self):
        session = _FakePycawSession(1, system=0, use_ctl=True)
        assert pl._session_is_system_sounds(session) is True

    def test_system_sounds_exception_is_false(self):
        class Boom:
            def IsSystemSoundsSession(self):
                raise RuntimeError("x")

        assert pl._session_is_system_sounds(Boom()) is False
        assert pl._session_is_system_sounds(object()) is False

    def test_list_audio_processes_unsupported_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pl, "supported", lambda: False)
        assert pl.list_audio_processes() == []

    def test_list_audio_processes_wrapper(self, monkeypatch):
        # 注入假 pycaw 模块：避免真实 pycaw 导入时的 COM 接口注册（公寓相关）
        monkeypatch.setattr(pl, "supported", lambda: True)
        monkeypatch.setattr(
            pl, "_resolve_process_name", lambda pid: f"proc{pid}.exe"
        )
        sessions = [
            _FakePycawSession(100, state=1, system=1),          # 活动普通进程
            _FakePycawSession(0, state=0, system=0),            # 系统声音（PID 0）
            _FakePycawSession(200, state=0, system=0, use_ctl=True),  # _ctl 判定系统声音
            _FakePycawSession(300, state=0, system=1),          # 普通进程
        ]

        fake_package = types.ModuleType("pycaw")
        fake_package.__path__ = []  # 标记为包，供 `from pycaw.pycaw import ...`
        fake_module = types.ModuleType("pycaw.pycaw")

        class _FakeAudioUtilities:
            @staticmethod
            def GetAllSessions():
                return sessions

        fake_module.AudioUtilities = _FakeAudioUtilities
        fake_package.pycaw = fake_module
        monkeypatch.setitem(sys.modules, "pycaw", fake_package)
        monkeypatch.setitem(sys.modules, "pycaw.pycaw", fake_module)

        entries = pl.list_audio_processes()
        assert [e["pid"] for e in entries] == [100, 300]
        assert entries[0]["active"] is True
        assert entries[0]["name"] == "proc100.exe"
        assert entries[0]["ordinal"] is None
