"""
按进程音频捕获集成测试：AudioCapture 双源 / watchdog / main 协议与回退编排

全部使用假 COM / 假进程 / 假音频，不依赖真实音频硬件。
"""
import asyncio
import threading
import types
from unittest.mock import AsyncMock

import numpy as np
import psutil
import pytest

import audio_capture as ac_mod
import main as main_mod
import process_loopback as pl
from audio_capture import AudioCapture
from main import SubtitleTranslator


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #

class FakeMic:
    def __init__(self, mic_id, name, isloopback=True):
        self.id = mic_id
        self.name = name
        self.isloopback = isloopback

    def recorder(self, **_kwargs):
        return object()


class FakeProc:
    def __init__(self, name, create_time=100.0):
        self._name = name
        self._create_time = create_time

    def name(self):
        return self._name

    def create_time(self):
        return self._create_time


class FakeProcessCapture:
    """假 ProcessLoopbackCapture：记录 pid/callback 与 start/stop 调用。"""

    instances = []

    def __init__(self, pid, callback):
        self.pid = pid
        self.callback = callback
        self.started = False
        self.stopped = False
        FakeProcessCapture.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeAudioCapture:
    """main 层交互替身（不触碰真实捕获）。"""

    def __init__(self):
        self.source = {'kind': 'device', 'id': ''}
        self.set_calls = []
        self.restart_count = 0
        self.lost_callback = None

    def set_audio_source(self, source):
        self.set_calls.append(source)
        if source is None:
            raise ValueError('未提供音频源')
        if isinstance(source, str):
            self.source = {'kind': 'device', 'id': source}
            return
        if not isinstance(source, dict):
            raise ValueError(f'非法的音频源格式: {source!r}')
        kind = source.get('kind', 'device')
        if kind == 'process':
            self.source = {
                'kind': 'process', 'name': source.get('name'), 'pid': source.get('pid')
            }
        elif kind == 'device':
            self.source = {'kind': 'device', 'id': source.get('id') or ''}
        else:
            raise ValueError(f'未知的音频源类型: {kind!r}')

    async def restart(self):
        self.restart_count += 1

    def get_current_source(self):
        return self.source

    def set_source_lost_callback(self, callback):
        self.lost_callback = callback

    def pause(self):
        pass

    def resume(self):
        pass


def make_app():
    app = SubtitleTranslator(config_path='nonexistent-config.yaml')
    app.websocket_server.send = AsyncMock()
    return app


def make_app_with_fake_capture():
    app = make_app()
    fake = FakeAudioCapture()
    app.audio_capture = fake
    return app, fake


def sent(app):
    return [c.args[0] for c in app.websocket_server.send.call_args_list]


# --------------------------------------------------------------------------- #
# AudioCapture：源校验与状态
# --------------------------------------------------------------------------- #

class TestSetAudioSource:
    def test_legacy_empty_string_is_default_device(self):
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source('')
        assert cap.get_current_source() == {'kind': 'device', 'id': ''}

    def test_legacy_device_id_resolves(self, monkeypatch):
        monkeypatch.setattr(
            ac_mod.sc, 'all_microphones',
            lambda include_loopback=True: [FakeMic('{dev-1}', '扬声器 (Realtek)')],
        )
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source('{dev-1}')
        assert cap._microphone is not None
        assert cap._microphone.id == '{dev-1}'
        assert cap.get_current_source() == {'kind': 'device', 'id': '{dev-1}'}

    def test_structured_device_source(self, monkeypatch):
        monkeypatch.setattr(
            ac_mod.sc, 'all_microphones',
            lambda include_loopback=True: [FakeMic('{dev-2}', 'TS24-40')],
        )
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source({'kind': 'device', 'id': '{dev-2}'})
        assert cap.get_current_source() == {'kind': 'device', 'id': '{dev-2}'}

    def test_unknown_device_raises_and_state_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            ac_mod.sc, 'all_microphones',
            lambda include_loopback=True: [FakeMic('{dev-1}', 'A')],
        )
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source('{dev-1}')
        with pytest.raises(ValueError, match='未找到音频源'):
            cap.set_audio_source('{missing}')
        assert cap.get_current_source() == {'kind': 'device', 'id': '{dev-1}'}

    def test_none_is_invalid(self):
        cap = AudioCapture({'audio': {}})
        with pytest.raises(ValueError):
            cap.set_audio_source(None)

    def test_non_str_non_dict_is_invalid(self):
        cap = AudioCapture({'audio': {}})
        with pytest.raises(ValueError):
            cap.set_audio_source(123)

    def test_process_source_valid_records_create_time(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(
            ac_mod.psutil, 'Process', lambda pid: FakeProc('chrome.exe', 42.5)
        )
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source({'kind': 'process', 'pid': 4321, 'name': 'chrome.exe'})
        assert cap.get_current_source() == {
            'kind': 'process', 'name': 'chrome.exe', 'pid': 4321
        }
        assert cap._process_create_time == 42.5
        assert cap._source_kind == 'process'

    def test_process_name_match_is_case_insensitive(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(
            ac_mod.psutil, 'Process', lambda pid: FakeProc('Chrome.EXE')
        )
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source({'kind': 'process', 'pid': 7, 'name': 'chrome.exe'})
        assert cap.get_current_source()['pid'] == 7

    def test_process_unsupported_raises(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: False)
        cap = AudioCapture({'audio': {}})
        with pytest.raises(ValueError, match='不支持'):
            cap.set_audio_source({'kind': 'process', 'pid': 1, 'name': 'x.exe'})

    def test_process_dead_pid_raises(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)

        def _no_such(pid):
            raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(ac_mod.psutil, 'Process', _no_such)
        cap = AudioCapture({'audio': {}})
        with pytest.raises(ValueError, match='进程不存在'):
            cap.set_audio_source({'kind': 'process', 'pid': 999, 'name': 'x.exe'})

    def test_process_name_mismatch_raises_state_unchanged(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(
            ac_mod.psutil, 'Process', lambda pid: FakeProc('other.exe')
        )
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source('')  # 默认设备
        with pytest.raises(ValueError, match='名称不匹配'):
            cap.set_audio_source({'kind': 'process', 'pid': 5, 'name': 'x.exe'})
        assert cap.get_current_source() == {'kind': 'device', 'id': ''}

    def test_process_bad_pid_or_name(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        cap = AudioCapture({'audio': {}})
        with pytest.raises(ValueError):
            cap.set_audio_source({'kind': 'process', 'pid': 0, 'name': 'x.exe'})
        with pytest.raises(ValueError):
            cap.set_audio_source({'kind': 'process', 'pid': 3, 'name': ''})

    def test_unknown_kind_raises(self):
        cap = AudioCapture({'audio': {}})
        with pytest.raises(ValueError, match='未知的音频源类型'):
            cap.set_audio_source({'kind': 'bogus'})


# --------------------------------------------------------------------------- #
# 默认回环设备选择（修复：优先系统默认扬声器，而非枚举首个）
# --------------------------------------------------------------------------- #

_REALTEK = '扬声器 (Realtek(R) Audio)'
_NVIDIA = 'TS24-40 (NVIDIA High Definition Audio)'


class TestPickDefaultLoopback:
    def test_prefers_default_speaker_name(self, monkeypatch):
        monkeypatch.setattr(
            ac_mod.sc, 'default_speaker',
            lambda: types.SimpleNamespace(name=_REALTEK),
        )
        mics = [FakeMic('{nv}', _NVIDIA), FakeMic('{rt}', _REALTEK)]
        cap = AudioCapture({'audio': {}})
        assert cap._pick_default_loopback(mics).id == '{rt}'

    def test_falls_back_when_default_speaker_raises(self, monkeypatch):
        def _boom():
            raise RuntimeError('no default device')

        monkeypatch.setattr(ac_mod.sc, 'default_speaker', _boom)
        mics = [FakeMic('{nv}', _NVIDIA), FakeMic('{rt}', _REALTEK)]
        cap = AudioCapture({'audio': {}})
        assert cap._pick_default_loopback(mics).id == '{nv}'  # 既有首个回环行为

    def test_falls_back_when_no_name_match(self, monkeypatch):
        monkeypatch.setattr(
            ac_mod.sc, 'default_speaker',
            lambda: types.SimpleNamespace(name='不存在的设备'),
        )
        mics = [FakeMic('{nv}', _NVIDIA), FakeMic('{rt}', _REALTEK)]
        cap = AudioCapture({'audio': {}})
        assert cap._pick_default_loopback(mics).id == '{nv}'

    def test_falls_back_when_default_speaker_is_none(self, monkeypatch):
        monkeypatch.setattr(ac_mod.sc, 'default_speaker', lambda: None)
        mics = [FakeMic('{nv}', _NVIDIA)]
        cap = AudioCapture({'audio': {}})
        assert cap._pick_default_loopback(mics).id == '{nv}'

    def test_explicit_device_path_untouched(self, monkeypatch):
        mics = [FakeMic('{nv}', _NVIDIA), FakeMic('{rt}', _REALTEK)]
        monkeypatch.setattr(
            ac_mod.sc, 'all_microphones',
            lambda include_loopback=True: mics,
        )

        def _boom():
            raise AssertionError('显式设备路径不得调用 default_speaker')

        monkeypatch.setattr(ac_mod.sc, 'default_speaker', _boom)
        cap = AudioCapture({'audio': {}})
        cap.set_audio_source('{nv}')  # 显式指定 NVIDIA（非默认扬声器）
        assert cap._microphone.id == '{nv}'

    async def test_start_device_capture_uses_default_speaker(self, monkeypatch):
        mics = [FakeMic('{nv}', _NVIDIA), FakeMic('{rt}', _REALTEK)]
        monkeypatch.setattr(
            ac_mod.sc, 'all_microphones',
            lambda include_loopback=True: mics,
        )
        monkeypatch.setattr(
            ac_mod.sc, 'default_speaker',
            lambda: types.SimpleNamespace(name=_REALTEK),
        )
        cap = AudioCapture({'audio': {}})
        cap._record_loop = lambda: None  # 不启动真实录音
        await cap._start_device_capture()
        assert cap._microphone.name == _REALTEK


# --------------------------------------------------------------------------- #
# AudioCapture：进程路径 start/stop + 回调 + 暂停
# --------------------------------------------------------------------------- #

class TestProcessCapturePath:
    def _make_process_cap(self):
        cap = AudioCapture({'audio': {}})
        cap._source_kind = 'process'
        cap._process_pid = 999
        cap._process_name = 'chrome.exe'
        cap._process_create_time = 1.0
        return cap

    async def test_start_process_uses_capture_and_arms_watchdog(self, monkeypatch):
        FakeProcessCapture.instances.clear()
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(ac_mod, 'ProcessLoopbackCapture', FakeProcessCapture)
        monkeypatch.setattr(ac_mod.psutil, 'Process', lambda pid: FakeProc('chrome.exe', 1.0))

        cap = self._make_process_cap()
        cap._watchdog_interval_s = 10.0
        try:
            await cap.start(lambda a: None)
            assert FakeProcessCapture.instances[-1].pid == 999
            assert FakeProcessCapture.instances[-1].started is True
            assert cap._process_capture is not None
            assert cap._watchdog_thread is not None
        finally:
            await cap.stop()
        assert FakeProcessCapture.instances[-1].stopped is True
        assert cap._process_capture is None
        assert cap._watchdog_stop is None

    async def test_unsupported_process_falls_back_to_device(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: False)
        cap = self._make_process_cap()
        cap._start_device_capture = AsyncMock()
        await cap.start(lambda a: None)
        assert cap.get_current_source() == {'kind': 'device', 'id': ''}
        cap._start_device_capture.assert_awaited_once()

    async def test_process_start_failure_falls_back_to_device(self, monkeypatch):
        class BoomCapture:
            def __init__(self, pid, callback):
                pass

            def start(self):
                raise RuntimeError('激活失败')

            def stop(self):
                pass

        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(ac_mod, 'ProcessLoopbackCapture', BoomCapture)
        cap = self._make_process_cap()
        cap._start_device_capture = AsyncMock()
        await cap.start(lambda a: None)  # MUST NOT raise
        assert cap.get_current_source() == {'kind': 'device', 'id': ''}
        cap._start_device_capture.assert_awaited_once()

    async def test_restart_preserves_pause(self, monkeypatch):
        FakeProcessCapture.instances.clear()
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(ac_mod, 'ProcessLoopbackCapture', FakeProcessCapture)
        monkeypatch.setattr(ac_mod.psutil, 'Process', lambda pid: FakeProc('chrome.exe', 1.0))

        cap = self._make_process_cap()
        cap._watchdog_interval_s = 10.0
        await cap.start(lambda a: None)
        cap.pause()
        try:
            await cap.restart()
            assert cap.is_paused is True
            assert cap._paused is True
        finally:
            await cap.stop()

    def test_process_audio_runs_existing_pipeline(self):
        cap = AudioCapture({'audio': {}})
        received = []
        cap._callback = received.append
        stereo = np.zeros((4800, 2), dtype=np.float32)
        stereo[:, 0] = 0.25
        cap._on_process_audio(stereo)
        assert len(received) == 1
        frame = received[0]
        assert frame.dtype == np.float32
        assert frame.ndim == 1
        assert abs(len(frame) - 1600) <= 2  # 48k→16k 单声道

    def test_process_audio_dropped_while_paused(self):
        cap = AudioCapture({'audio': {}})
        received = []
        cap._callback = received.append
        stereo = np.zeros((480, 2), dtype=np.float32)
        cap.pause()
        cap._on_process_audio(stereo)
        assert received == []
        cap.resume()
        cap._on_process_audio(stereo)
        assert len(received) == 1


# --------------------------------------------------------------------------- #
# AudioCapture：watchdog
# --------------------------------------------------------------------------- #

class TestWatchdog:
    def _armed_cap(self, monkeypatch, process_factory):
        monkeypatch.setattr(ac_mod.psutil, 'Process', process_factory)
        cap = AudioCapture({'audio': {}})
        cap._source_kind = 'process'
        cap._process_pid = 1234
        cap._process_name = 'chrome.exe'
        cap._process_create_time = 100.0
        cap._watchdog_interval_s = 0.02
        return cap

    def test_create_time_mismatch_triggers_loss(self, monkeypatch):
        cap = self._armed_cap(monkeypatch, lambda pid: FakeProc('chrome.exe', 200.0))
        lost = []
        fired = threading.Event()

        def cb(name, pid):
            lost.append((name, pid))
            fired.set()

        cap._on_source_lost = cb
        cap._arm_watchdog()
        try:
            assert fired.wait(1.0) is True
        finally:
            cap._disarm_watchdog()
        assert lost == [('chrome.exe', 1234)]

    def test_process_gone_triggers_loss(self, monkeypatch):
        def _no_such(pid):
            raise psutil.NoSuchProcess(pid)

        cap = self._armed_cap(monkeypatch, _no_such)
        fired = threading.Event()
        cap._on_source_lost = lambda name, pid: fired.set()
        cap._arm_watchdog()
        try:
            assert fired.wait(1.0) is True
        finally:
            cap._disarm_watchdog()

    def test_alive_process_does_not_trigger(self, monkeypatch):
        cap = self._armed_cap(monkeypatch, lambda pid: FakeProc('chrome.exe', 100.0))
        fired = threading.Event()
        cap._on_source_lost = lambda name, pid: fired.set()
        cap._arm_watchdog()
        try:
            assert fired.wait(0.2) is False  # 多个轮询周期内不应误报
        finally:
            cap._disarm_watchdog()

    def test_watchdog_survives_pause(self, monkeypatch):
        cap = self._armed_cap(monkeypatch, lambda pid: FakeProc('chrome.exe', 200.0))
        fired = threading.Event()
        cap._on_source_lost = lambda name, pid: fired.set()
        cap.pause()
        cap._arm_watchdog()
        try:
            assert fired.wait(1.0) is True  # 暂停不解除 watchdog
        finally:
            cap._disarm_watchdog()

    def test_disarm_stops_watchdog(self, monkeypatch):
        cap = self._armed_cap(monkeypatch, lambda pid: FakeProc('chrome.exe', 100.0))
        cap._on_source_lost = lambda name, pid: None
        cap._arm_watchdog()
        thread = cap._watchdog_thread
        cap._disarm_watchdog()
        thread.join(timeout=1.0)
        assert not thread.is_alive()
        assert cap._watchdog_thread is None


# --------------------------------------------------------------------------- #
# main：协议扩展
# --------------------------------------------------------------------------- #

class TestMainProtocol:
    async def test_get_audio_processes_supported(self, monkeypatch):
        app = make_app()
        monkeypatch.setattr(main_mod.process_loopback, 'supported', lambda: True)
        monkeypatch.setattr(
            main_mod.process_loopback, 'list_audio_processes',
            lambda: [{'pid': 10, 'name': 'chrome.exe', 'active': True, 'ordinal': None}],
        )
        result = await app._method_get_audio_processes(None)
        assert result == {
            'supported': True,
            'reason': None,
            'processes': [
                {'pid': 10, 'name': 'chrome.exe', 'active': True, 'ordinal': None}
            ],
        }

    async def test_get_audio_processes_unsupported(self, monkeypatch):
        app = make_app()
        monkeypatch.setattr(main_mod.process_loopback, 'supported', lambda: False)
        result = await app._method_get_audio_processes(None)
        assert result == {'supported': False, 'reason': 'os_too_old', 'processes': []}

    async def test_get_audio_processes_enumeration_failure(self, monkeypatch):
        app = make_app()
        monkeypatch.setattr(main_mod.process_loopback, 'supported', lambda: True)

        def _boom():
            raise pl.ProcessLoopbackError('枚举炸了')

        monkeypatch.setattr(main_mod.process_loopback, 'list_audio_processes', _boom)
        with pytest.raises(RuntimeError) as excinfo:
            await app._method_get_audio_processes(None)
        assert 'enumerate_failed' in str(excinfo.value)

    async def test_get_config_includes_audio_source_default(self):
        app = make_app()
        result = await app._method_get_config(None)
        assert result['audio']['source'] == {'kind': 'device', 'id': ''}

    async def test_get_config_reflects_process_source(self):
        app = make_app()
        result = await app._method_get_config(None)
        assert result['audio']['source']['kind'] == 'device'

        app.audio_capture._source_kind = 'process'
        app.audio_capture._process_pid = 77
        app.audio_capture._process_name = 'chrome.exe'
        result = await app._method_get_config(None)
        assert result['audio']['source'] == {
            'kind': 'process', 'name': 'chrome.exe', 'pid': 77
        }


# --------------------------------------------------------------------------- #
# main：set_audio_source 分发
# --------------------------------------------------------------------------- #

class TestSetAudioSourceDispatch:
    async def test_legacy_source_id(self):
        app, fake = make_app_with_fake_capture()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source', 'source_id': '{dev}'
        })
        assert fake.set_calls == ['{dev}']
        assert fake.restart_count == 1
        assert not any(m.get('type') == 'error' for m in sent(app))

    async def test_structured_process_source(self):
        app, fake = make_app_with_fake_capture()
        source = {'kind': 'process', 'pid': 42, 'name': 'chrome.exe'}
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source', 'source': source
        })
        assert fake.set_calls == [source]
        assert fake.restart_count == 1

    async def test_invalid_source_error_receipt_no_restart(self):
        app, fake = make_app_with_fake_capture()

        def _reject(_source):
            raise ValueError('进程不存在: chrome.exe (pid=1)')

        fake.set_audio_source = _reject
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source',
            'source': {'kind': 'process', 'pid': 1, 'name': 'chrome.exe'},
        })
        errors = [m for m in sent(app) if m.get('type') == 'error']
        assert len(errors) == 1
        assert errors[0]['code'] == 'invalid_audio_source'
        assert fake.restart_count == 0

    async def test_missing_source_is_invalid(self):
        app, fake = make_app_with_fake_capture()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source'
        })
        errors = [m for m in sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_audio_source'
        assert fake.restart_count == 0


class TestInvalidAudioSourceViaControl:
    """真实 AudioCapture + monkeypatched psutil → invalid_audio_source 回执。"""

    async def test_dead_pid(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)

        def _no_such(pid):
            raise psutil.NoSuchProcess(pid)

        monkeypatch.setattr(ac_mod.psutil, 'Process', _no_such)
        app = make_app()
        app.audio_capture.restart = AsyncMock()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source',
            'source': {'kind': 'process', 'pid': 999, 'name': 'chrome.exe'},
        })
        errors = [m for m in sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_audio_source'
        app.audio_capture.restart.assert_not_awaited()

    async def test_name_mismatch(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(
            ac_mod.psutil, 'Process', lambda pid: FakeProc('other.exe')
        )
        app = make_app()
        app.audio_capture.restart = AsyncMock()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source',
            'source': {'kind': 'process', 'pid': 5, 'name': 'chrome.exe'},
        })
        errors = [m for m in sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_audio_source'
        app.audio_capture.restart.assert_not_awaited()

    async def test_unsupported_process(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: False)
        app = make_app()
        app.audio_capture.restart = AsyncMock()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source',
            'source': {'kind': 'process', 'pid': 5, 'name': 'chrome.exe'},
        })
        errors = [m for m in sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_audio_source'
        app.audio_capture.restart.assert_not_awaited()

    async def test_valid_process_restarts_and_keeps_source(self, monkeypatch):
        monkeypatch.setattr(ac_mod, 'process_loopback_supported', lambda: True)
        monkeypatch.setattr(
            ac_mod.psutil, 'Process', lambda pid: FakeProc('Chrome.EXE', 9.0)
        )
        app = make_app()
        app.audio_capture.restart = AsyncMock()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source',
            'source': {'kind': 'process', 'pid': 7, 'name': 'chrome.exe'},
        })
        assert not any(m.get('type') == 'error' for m in sent(app))
        assert app.audio_capture.get_current_source() == {
            'kind': 'process', 'name': 'chrome.exe', 'pid': 7
        }
        app.audio_capture.restart.assert_awaited_once()


# --------------------------------------------------------------------------- #
# main：退出回退编排
# --------------------------------------------------------------------------- #

class TestSourceLostFallback:
    async def test_handle_source_lost_falls_back_and_broadcasts(self):
        app, fake = make_app_with_fake_capture()
        await app._handle_source_lost('chrome.exe', 1234)
        assert fake.set_calls == [{'kind': 'device', 'id': ''}]
        assert fake.restart_count == 1
        msg = app.websocket_server.send.call_args.args[0]
        assert msg == {
            'type': 'audio_source_lost',
            'name': 'chrome.exe',
            'pid': 1234,
            'fallback': 'system',
        }

    async def test_handle_source_lost_with_real_server_no_clients(self):
        # 不替换 send：走真实 WebSocketServer.send 的无客户端快速返回路径
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        app.audio_capture = FakeAudioCapture()
        await app._handle_source_lost('chrome.exe', 5)  # MUST NOT raise
        assert app.websocket_server.get_client_count() == 0

    async def test_on_audio_source_lost_schedules_via_loop(self):
        app, fake = make_app_with_fake_capture()
        app._loop = asyncio.get_running_loop()
        app._on_audio_source_lost('chrome.exe', 9)
        await asyncio.sleep(0.05)  # 让 call_soon_threadsafe + task 跑完
        assert fake.restart_count == 1
        assert app.websocket_server.send.await_count == 1

    def test_on_audio_source_lost_without_loop_is_safe(self):
        app, fake = make_app_with_fake_capture()
        app._loop = None
        app._on_audio_source_lost('chrome.exe', 9)  # MUST NOT raise
        assert fake.restart_count == 0

    async def test_fallback_survives_restart_failure(self):
        app, fake = make_app_with_fake_capture()

        async def _boom():
            raise RuntimeError('restart 失败')

        fake.restart = _boom
        await app._handle_source_lost('chrome.exe', 3)  # MUST NOT raise
        assert app.websocket_server.send.await_count == 1
