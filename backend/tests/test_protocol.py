"""
WebSocket 请求/响应协议与激活语言控制测试
"""
import asyncio
import json
import logging
import types
from unittest.mock import AsyncMock

import httpx
import pytest
import websockets

import asr_engine as asr_mod
import translator as tr_mod
from asr_engine import ASREngine
from main import SubtitleTranslator
from model_downloader import DownloadError
from pipeline_worker import PipelineWorker
from translation_models import model_path
from translator import Translator
from websocket_server import WebSocketServer

TEST_PORT = 18765


async def ws_request(ws, method, params=None):
    """发送请求并等待响应"""
    await ws.send(json.dumps({
        'type': 'request', 'id': 'r1', 'method': method, 'params': params
    }))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        if msg.get('type') == 'response':
            return msg


class TestRequestResponse:
    async def test_request_response_roundtrip(self):
        server = WebSocketServer({'websocket': {'host': 'localhost', 'port': TEST_PORT}})
        server.register_method('echo', AsyncMock(return_value={'pong': 1}))
        await server.start(AsyncMock())
        try:
            async with websockets.connect(f'ws://localhost:{TEST_PORT}') as ws:
                resp = await ws_request(ws, 'echo')
                assert resp['ok'] is True
                assert resp['id'] == 'r1'
                assert resp['result'] == {'pong': 1}
        finally:
            await server.stop()

    async def test_unknown_method_error(self):
        server = WebSocketServer({'websocket': {'host': 'localhost', 'port': TEST_PORT}})
        await server.start(AsyncMock())
        try:
            async with websockets.connect(f'ws://localhost:{TEST_PORT}') as ws:
                resp = await ws_request(ws, 'no_such_method')
                assert resp['ok'] is False
                assert '未知方法' in resp['error']
        finally:
            await server.stop()

    async def test_handler_exception_returns_error(self):
        server = WebSocketServer({'websocket': {'host': 'localhost', 'port': TEST_PORT}})
        server.register_method('boom', AsyncMock(side_effect=RuntimeError('炸了')))
        await server.start(AsyncMock())
        try:
            async with websockets.connect(f'ws://localhost:{TEST_PORT}') as ws:
                resp = await ws_request(ws, 'boom')
                assert resp['ok'] is False
                assert '炸了' in resp['error']
        finally:
            await server.stop()

    async def test_get_audio_sources_via_app(self):
        """应用装配后可通过 WS 拉取音频源"""
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        app.config['websocket']['port'] = TEST_PORT
        app.websocket_server = WebSocketServer(app.config)
        app.websocket_server.register_method(
            'get_audio_sources', app._method_get_audio_sources
        )
        await app.websocket_server.start(AsyncMock())
        try:
            async with websockets.connect(f'ws://localhost:{TEST_PORT}') as ws:
                resp = await ws_request(ws, 'get_audio_sources')
                assert resp['ok'] is True
                assert isinstance(resp['result'], list)
        finally:
            await app.websocket_server.stop()


class TestActiveLanguage:
    def make_app(self):
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        app.websocket_server.send = AsyncMock()
        return app

    async def test_set_language_valid(self):
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'set_language', 'language': 'en'
        })
        assert app.active_target_language == 'en'

    async def test_set_language_invalid_receipt(self):
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'set_language', 'language': 'xx'
        })
        assert app.active_target_language == 'zh'  # 未变
        error_msgs = [
            c.args[0] for c in app.websocket_server.send.call_args_list
            if c.args[0].get('type') == 'error'
        ]
        assert error_msgs and error_msgs[0]['code'] == 'invalid_language'

    async def test_config_sync(self):
        app = self.make_app()
        await app._on_control_message({
            'type': 'config_sync',
            'target_languages': ['en', 'ja'],
            'active_language': 'ja'
        })
        assert app.translator.target_languages == ['en', 'ja']
        assert app.active_target_language == 'ja'

    async def test_config_sync_invalid_active_ignored(self):
        app = self.make_app()
        await app._on_control_message({
            'type': 'config_sync',
            'target_languages': ['en', 'ja'],
            'active_language': 'fr'  # 不在目标列表
        })
        assert app.active_target_language == 'zh'  # 保持默认

    async def test_subtitle_carries_active_language(self):
        """subtitle 消息携带 active_language，且只翻译激活语言"""
        app = self.make_app()
        app.active_target_language = 'en'

        asr = AsyncMock()
        asr.is_ready = True
        asr.transcribe = AsyncMock(return_value={'text': 'こんにちは', 'language': 'ja'})
        translator = AsyncMock()
        translator.translate_with_metrics = AsyncMock(
            return_value=({'en': 'hello'}, {'en': 33.3})
        )
        ws = AsyncMock()
        ws.send = AsyncMock()

        worker = PipelineWorker(
            asr, translator, ws, queue_size=2,
            get_active_language=lambda: app.active_target_language
        )
        await worker.start()
        try:
            import numpy as np
            worker.submit(np.zeros(16000, dtype=np.float32))
            for _ in range(100):
                if any(
                    c.args[0].get('type') == 'subtitle'
                    for c in ws.send.call_args_list
                ):
                    break
                await asyncio.sleep(0.01)
        finally:
            await worker.stop()

        # 只翻译激活语言
        translator.translate_with_metrics.assert_called_once_with('こんにちは', 'ja', ['en'])
        msg = [c.args[0] for c in ws.send.call_args_list
               if c.args[0].get('type') == 'subtitle'][0]
        assert msg['active_language'] == 'en'
        assert msg['translations'] == {'en': 'hello'}
        assert msg['tps'] == 33.3


def _probe(cuda_available: bool, detail: str = 'test-probe') -> dict:
    return {'cuda_available': cuda_available, 'source': 'fake', 'detail': detail}


class _FakeStream:
    def __init__(self):
        self._text = ''

    def accept_waveform(self, sample_rate, samples):
        pass

    @property
    def result(self):
        return types.SimpleNamespace(text=self._text)


class _FakeRecognizer:
    """假识别器：满足端到端热身验证的最小契约（decode → 空文本）"""

    def __init__(self, provider='cpu'):
        self.provider = provider

    def create_stream(self):
        return _FakeStream()

    def decode_stream(self, stream):
        stream._text = ''


class TestASREngineDevice:
    """ASR 统一加载路径 + change_device（假识别器，无网络）"""

    def make_engine(self, monkeypatch, cuda_available, fail_cuda=False,
                    device='auto'):
        created = []

        def fake_builder(provider):
            created.append(provider)
            if provider == 'cuda' and fail_cuda:
                raise RuntimeError(
                    'CUDA provider unavailable: no CUDA-capable device'
                )
            return _FakeRecognizer(provider)

        monkeypatch.setattr(
            asr_mod, 'probe_compute', lambda: {'asr': _probe(cuda_available)}
        )
        monkeypatch.setattr(asr_mod, 'is_asr_model_downloaded', lambda: True)
        engine = ASREngine({'asr': {'device': device}})
        monkeypatch.setattr(engine, '_build_recognizer', fake_builder)
        return engine, created

    async def test_auto_with_cuda_uses_gpu(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=True)
        await engine.initialize()
        assert engine.resolved_device == 'cuda'
        assert engine.device_reason == 'auto'
        assert created == ['cuda']

    async def test_auto_without_cuda_uses_cpu(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=False)
        await engine.initialize()
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'no_cuda'
        assert created == ['cpu']

    async def test_explicit_cuda_probe_unavailable_skips_gpu(self, monkeypatch):
        engine, created = self.make_engine(
            monkeypatch, cuda_available=False, device='cuda'
        )
        await engine.initialize()
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'no_cuda'
        assert created == ['cpu']  # 未尝试注定失败的 GPU 加载

    async def test_explicit_cpu_never_tries_gpu(self, monkeypatch):
        engine, created = self.make_engine(
            monkeypatch, cuda_available=True, device='cpu'
        )
        await engine.initialize()
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'user'
        assert created == ['cpu']

    @pytest.mark.parametrize('device', ['auto', 'cuda'])
    async def test_gpu_load_failure_silent_degrade(self, monkeypatch, caplog, device):
        engine, created = self.make_engine(
            monkeypatch, cuda_available=True, fail_cuda=True, device=device
        )
        with caplog.at_level(logging.WARNING, logger='asr_engine'):
            await engine.initialize()  # MUST NOT raise

        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'load_failed'
        assert created == ['cuda', 'cpu']
        assert any('降级' in r.message for r in caplog.records)

    async def test_change_device_invalid_raises(self, monkeypatch):
        engine, _ = self.make_engine(monkeypatch, cuda_available=False)
        with pytest.raises(ValueError):
            await engine.change_device('tpu')

    async def test_change_device_same_value_idempotent(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=False)
        await engine.initialize()
        assert len(created) == 1

        await engine.change_device('auto')  # 与偏好一致 → 不重载
        assert len(created) == 1
        assert engine.resolved_device == 'cpu'

    async def test_change_device_switches_and_records_reason(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=False)
        await engine.initialize()
        assert engine.device_reason == 'no_cuda'

        await engine.change_device('cpu')
        assert engine.device == 'cpu'
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'user'
        assert created == ['cpu', 'cpu']

    async def test_change_device_to_cuda_without_probe_degrades(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=False)
        await engine.initialize()

        await engine.change_device('cuda')
        assert engine.device == 'cuda'
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'no_cuda'
        # 未新增 GPU 尝试
        assert 'cuda' not in created

    def test_get_model_info_keeps_preference_and_adds_resolved(self, monkeypatch):
        engine, _ = self.make_engine(monkeypatch, cuda_available=True)
        info = engine.get_model_info()
        assert info['device'] == 'auto'  # 配置偏好语义不变
        assert info['resolved_device'] is None
        assert info['device_reason'] is None
        assert 'resolved_device' in info and 'device_reason' in info
        # 单引擎模型语义（无 Whisper 档位字段）
        assert info['model'] == 'funasr-nano'
        assert info['language'] == 'ja'
        assert 'model_size' not in info
        assert 'supported_models' not in info


class _FakeLlamaManager:
    """test_protocol 用最小 llama-server 管理器替身"""

    def __init__(self):
        self.starts = []
        self.stop_calls = 0
        self.is_running = False
        self.is_ready = False
        self.port = 18081
        self.model_path = None

    async def start(self, config):
        self.starts.append(config)
        self.model_path = config.model_path
        self.is_running = True
        self.is_ready = True

    async def stop(self):
        self.stop_calls += 1
        self.is_running = False
        self.is_ready = False
        self.model_path = None


class TestTranslatorDevice:
    """翻译设备解析 + change_device（假探针/假管理器/假请求，无网络/无进程）"""

    def make_translator(self, monkeypatch, tmp_path, cuda_available, *, device='auto'):
        monkeypatch.setattr(
            tr_mod, 'probe_compute',
            lambda: {'translation': _probe(cuda_available)},
        )
        monkeypatch.setattr(
            tr_mod, 'is_downloaded', lambda model, cache_root=None: True
        )
        exe = tmp_path / 'llama-server.exe'
        exe.write_bytes(b'stub')
        monkeypatch.setattr(tr_mod, 'resolve_device_binary', lambda root, dev: exe)

        def handler(request):
            return httpx.Response(200, json={
                'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]
            })

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        manager = _FakeLlamaManager()
        translator = Translator(
            {'translation': {'device': device, 'target_languages': ['zh']}},
            manager=manager, client=client, cache_root=tmp_path,
        )
        return translator, manager

    def mark_loaded(self, translator, manager, tmp_path):
        """模拟 llama-server 已拉起"""
        manager.is_running = True
        manager.is_ready = True
        manager.model_path = model_path(translator._model, tmp_path)

    async def test_initialize_resolves_cpu_no_cuda(self, monkeypatch, tmp_path):
        tr, mgr = self.make_translator(monkeypatch, tmp_path, cuda_available=False)
        await tr.initialize()
        assert tr.device == 'auto'  # 偏好保持
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'no_cuda'
        assert mgr.starts == []  # 拉起由后台预载负责

    async def test_initialize_resolves_cuda_auto(self, monkeypatch, tmp_path):
        tr, mgr = self.make_translator(monkeypatch, tmp_path, cuda_available=True)
        await tr.initialize()
        assert tr.resolved_device == 'cuda'
        assert tr.device_reason == 'auto'

    def test_get_model_info_adds_fields(self, monkeypatch, tmp_path):
        tr, mgr = self.make_translator(monkeypatch, tmp_path, cuda_available=False)
        info = tr.get_model_info()
        assert info['device'] == 'auto'
        assert info['resolved_device'] is None
        assert info['device_reason'] is None
        assert 'resolved_device' in info and 'device_reason' in info

    async def test_change_device_invalid_raises(self, monkeypatch, tmp_path):
        tr, mgr = self.make_translator(monkeypatch, tmp_path, cuda_available=False)
        with pytest.raises(ValueError):
            await tr.change_device('tpu')

    async def test_change_device_same_value_idempotent(self, monkeypatch, tmp_path):
        tr, mgr = self.make_translator(monkeypatch, tmp_path, cuda_available=False)
        await tr.initialize()
        self.mark_loaded(tr, mgr, tmp_path)
        await tr.change_device('auto')
        assert mgr.stop_calls == 0  # 未终止
        assert tr.is_ready is True  # 未卸载

    async def test_change_device_resets_and_restarts(self, monkeypatch, tmp_path):
        tr, mgr = self.make_translator(monkeypatch, tmp_path, cuda_available=False)
        await tr.initialize()
        self.mark_loaded(tr, mgr, tmp_path)

        await tr.change_device('cuda')  # 探针不可用 → cpu/no_cuda；已加载 → 重新拉起

        assert tr.device == 'cuda'
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'no_cuda'
        assert mgr.stop_calls == 1
        assert mgr.starts and mgr.starts[-1].n_gpu_layers == 0  # CPU 构建

    async def test_change_device_unloaded_not_reloaded(self, monkeypatch, tmp_path):
        tr, mgr = self.make_translator(monkeypatch, tmp_path, cuda_available=False)
        await tr.initialize()
        await tr.change_device('cuda')
        assert tr.resolved_device == 'cpu'
        assert mgr.starts == []  # 切换前未加载 → 不后台重拉


class TestDeviceControl:
    """change_device 控制分发 + device_state 广播 + get_config 新字段"""

    def make_app(self):
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        app.websocket_server.send = AsyncMock()
        return app

    @staticmethod
    def sent(app):
        return [c.args[0] for c in app.websocket_server.send.call_args_list]

    async def test_change_device_invalid_receipt_no_state_change(self):
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'change_device', 'device': 'tpu'
        })
        msgs = self.sent(app)
        errors = [m for m in msgs if m.get('type') == 'error']
        assert len(errors) == 1
        assert errors[0]['code'] == 'invalid_device'
        assert not any(m.get('type') == 'device_state' for m in msgs)

    async def test_change_device_missing_value_is_invalid(self):
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'change_device'
        })
        errors = [m for m in self.sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_device'

    async def test_change_device_success_broadcasts_state(self):
        app = self.make_app()
        app.asr_engine = AsyncMock()
        app.asr_engine.resolved_device = 'cpu'
        app.asr_engine.device_reason = 'user'
        app.translator = AsyncMock()
        app.translator.resolved_device = 'cpu'
        app.translator.device_reason = 'no_cuda'

        await app._on_control_message({
            'type': 'control', 'action': 'change_device', 'device': 'cpu'
        })

        app.asr_engine.change_device.assert_awaited_once_with('cpu')
        app.translator.change_device.assert_awaited_once_with('cpu')
        msgs = self.sent(app)
        assert not any(m.get('type') == 'error' for m in msgs)
        states = [m for m in msgs if m.get('type') == 'device_state']
        assert len(states) == 1
        assert states[0]['asr'] == {'resolved': 'cpu', 'reason': 'user'}
        assert states[0]['translation'] == {'resolved': 'cpu', 'reason': 'no_cuda'}

    async def test_change_device_degradation_has_no_error_receipt(self):
        """cuda 请求静默降级：无 error 回执，device_state 反映 no_cuda"""
        app = self.make_app()
        app.asr_engine = AsyncMock()
        app.asr_engine.resolved_device = 'cpu'
        app.asr_engine.device_reason = 'no_cuda'
        app.translator = AsyncMock()
        app.translator.resolved_device = 'cpu'
        app.translator.device_reason = 'no_cuda'

        await app._on_control_message({
            'type': 'control', 'action': 'change_device', 'device': 'cuda'
        })

        msgs = self.sent(app)
        assert not any(m.get('type') == 'error' for m in msgs)
        state = [m for m in msgs if m.get('type') == 'device_state'][0]
        assert state['asr'] == {'resolved': 'cpu', 'reason': 'no_cuda'}

    async def test_change_device_internal_error_is_swallowed(self):
        """内部异常不冒泡（避免连接级 catch 断连），且非非法输入不回 error"""
        app = self.make_app()
        app.asr_engine.change_device = AsyncMock(side_effect=RuntimeError('boom'))

        await app._on_control_message({
            'type': 'control', 'action': 'change_device', 'device': 'cpu'
        })  # MUST NOT raise

        msgs = self.sent(app)
        assert not any(m.get('type') == 'error' for m in msgs)
        assert not any(m.get('type') == 'device_state' for m in msgs)

    async def test_broadcast_device_state_shape(self):
        app = self.make_app()
        app.asr_engine._resolved_device = 'cuda'
        app.asr_engine._device_reason = 'auto'
        app.translator._resolved_device = 'cpu'
        app.translator._device_reason = 'load_failed'

        await app._broadcast_device_state()

        msg = app.websocket_server.send.call_args.args[0]
        assert msg['type'] == 'device_state'
        assert msg['asr'] == {'resolved': 'cuda', 'reason': 'auto'}
        assert msg['translation'] == {'resolved': 'cpu', 'reason': 'load_failed'}

    async def test_broadcast_device_state_unresolved_is_null(self):
        app = self.make_app()
        await app._broadcast_device_state()
        msg = app.websocket_server.send.call_args.args[0]
        assert msg['asr'] == {'resolved': None, 'reason': None}
        assert msg['translation'] == {'resolved': None, 'reason': None}

    async def test_get_config_includes_device_fields(self):
        app = self.make_app()
        result = await app._method_get_config(None)
        assert result['asr']['device'] == app.asr_engine.device  # 偏好不变
        assert 'resolved_device' in result['asr']
        assert 'device_reason' in result['asr']
        assert 'resolved_device' in result['translation']
        assert 'device_reason' in result['translation']

    async def test_get_config_translation_registry_fields(self):
        app = self.make_app()
        result = await app._method_get_config(None)
        tr = result['translation']
        assert tr['model'] == 'hy-mt2-1.8b-q4km'
        assert [m['id'] for m in tr['available_models']]
        assert 'target_languages' in tr
        # NLLB 旧字段不得再出现
        assert 'primary_model' not in tr
        assert 'fallback_model' not in tr
        assert 'nllb_loaded' not in tr
        assert 'nllb_languages' not in tr

    async def test_broadcast_with_no_clients_is_safe(self):
        """真实 WebSocketServer 无客户端时广播 MUST NOT 抛异常"""
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        await app._broadcast_device_state()
        assert app.websocket_server.get_client_count() == 0

    async def test_preload_default_broadcasts_device_state(self):
        app = self.make_app()
        app.translator.ensure_default = AsyncMock()
        app.translator._resolved_device = 'cpu'
        app.translator._device_reason = 'no_cuda'

        await app._preload_default_model()

        types = [m.get('type') for m in self.sent(app)]
        assert 'device_state' in types

    async def test_preload_failure_still_broadcasts_and_does_not_raise(self):
        app = self.make_app()
        app.translator.ensure_default = AsyncMock(side_effect=RuntimeError('net down'))

        await app._preload_default_model()  # MUST NOT raise

        types = [m.get('type') for m in self.sent(app)]
        assert 'device_state' in types

    async def test_start_broadcasts_after_asr_initialize(self):
        app = self.make_app()
        app.websocket_server.start = AsyncMock()
        app.asr_engine.initialize = AsyncMock()
        app.asr_engine._resolved_device = 'cpu'
        app.asr_engine._device_reason = 'no_cuda'
        app.translator.initialize = AsyncMock()
        app.translator.ensure_default = AsyncMock()
        app.translator._resolved_device = 'cpu'
        app.translator._device_reason = 'no_cuda'
        app.worker.start = AsyncMock()
        app.audio_capture.start = AsyncMock()
        app._segmentation_loop = AsyncMock()

        await app.start()
        try:
            if app._preload_task is not None:
                await app._preload_task
            states = [m for m in self.sent(app) if m.get('type') == 'device_state']
            assert states
            assert states[0]['asr'] == {'resolved': 'cpu', 'reason': 'no_cuda'}
        finally:
            await app.stop()

    async def test_runtime_degraded_broadcasts_state_and_warning(self):
        """运行期降级（runtime_failed）→ device_state + engine_degraded 告警"""
        app = self.make_app()
        app.asr_engine._resolved_device = 'cpu'
        app.asr_engine._device_reason = 'runtime_failed'

        await app._handle_asr_health({
            'event': 'runtime_degraded',
            'resolved': 'cpu',
            'reason': 'runtime_failed',
            'model': 'funasr-nano',
        })

        msgs = self.sent(app)
        states = [m for m in msgs if m.get('type') == 'device_state']
        assert len(states) == 1
        assert states[0]['asr'] == {'resolved': 'cpu', 'reason': 'runtime_failed'}

        warnings = [m for m in msgs if m.get('type') == 'pipeline_warning']
        assert len(warnings) == 1
        assert warnings[0]['reason'] == 'engine_degraded'
        assert warnings[0]['detail'] == {
            'engine': 'asr', 'from': 'cuda', 'to': 'cpu',
            'device_reason': 'runtime_failed',
        }
        assert '自动回退 CPU' in warnings[0]['message']
        assert 'dropped' in warnings[0]

    async def test_persistent_failure_warning_payload(self):
        """CPU 持续失败 → 仅 engine_degraded 告警，无 device_state"""
        app = self.make_app()
        await app._handle_asr_health({
            'event': 'persistent_failure',
            'failures': 3,
            'last_error': 'boom',
        })

        msgs = self.sent(app)
        assert not any(m.get('type') == 'device_state' for m in msgs)
        warnings = [m for m in msgs if m.get('type') == 'pipeline_warning']
        assert len(warnings) == 1
        assert warnings[0]['reason'] == 'engine_degraded'
        assert warnings[0]['detail']['failures'] == 3


class TestModelAndSourceLanguageControl:
    """change_model（单引擎语义）与 set_source_language 控制分发"""

    def make_app(self):
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        app.websocket_server.send = AsyncMock()
        return app

    @staticmethod
    def sent(app):
        return [c.args[0] for c in app.websocket_server.send.call_args_list]

    async def test_change_model_invalid_receipt(self):
        """旧档位名等其他值 → invalid_model 回执（协议兼容）"""
        app = self.make_app()
        app.asr_engine = AsyncMock()
        app.asr_engine.change_model = AsyncMock(side_effect=ValueError('不支持'))

        await app._on_control_message({
            'type': 'control', 'action': 'change_model', 'model_size': 'small'
        })

        errors = [m for m in self.sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_model'
        app.asr_engine.change_model.assert_awaited_once_with('small')

    async def test_change_model_valid_idempotent(self):
        app = self.make_app()
        app.asr_engine = AsyncMock()

        await app._on_control_message({
            'type': 'control', 'action': 'change_model', 'model_size': 'funasr-nano'
        })

        app.asr_engine.change_model.assert_awaited_once_with('funasr-nano')
        assert not [m for m in self.sent(app) if m.get('type') == 'error']

    async def test_set_source_language_valid(self):
        app = self.make_app()

        await app._on_control_message({
            'type': 'control', 'action': 'set_source_language', 'language': 'zh'
        })

        assert app.asr_engine.source_language == 'zh'
        assert not [m for m in self.sent(app) if m.get('type') == 'error']

    async def test_set_source_language_invalid_receipt(self):
        """非法语言 → invalid_language 回执，源语言保持不变"""
        app = self.make_app()

        await app._on_control_message({
            'type': 'control', 'action': 'set_source_language', 'language': 'ko'
        })

        assert app.asr_engine.source_language == 'ja'  # 未变
        errors = [m for m in self.sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_language'

    async def test_get_config_asr_section_single_model_shape(self):
        """get_config asr 段：model/language 字段，无 Whisper 档位字段"""
        app = self.make_app()
        result = await app._method_get_config(None)
        asr = result['asr']
        assert asr['model'] == 'funasr-nano'
        assert asr['language'] == 'ja'
        assert 'resolved_device' in asr
        assert 'device_reason' in asr
        assert 'model_size' not in asr
        assert 'supported_models' not in asr


class TestAudioSourceControl:
    """set_audio_source 控制分发（设备-only 结构化；进程形态已随回退移除）"""

    def make_app(self):
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        app.websocket_server.send = AsyncMock()
        return app

    @staticmethod
    def sent(app):
        return [c.args[0] for c in app.websocket_server.send.call_args_list]

    async def test_device_source_switch_accepted(self):
        """结构化设备源（id=''=默认设备）被接受，无错误回执"""
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source',
            'source': {'kind': 'device', 'id': ''}
        })
        assert not any(m.get('type') == 'error' for m in self.sent(app))
        assert app.audio_capture.get_current_source() == {'kind': 'device', 'id': ''}

    async def test_legacy_source_id_accepted(self):
        """旧格式裸设备 id（''）继续按设备源接受"""
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source', 'source_id': ''
        })
        assert not any(m.get('type') == 'error' for m in self.sent(app))

    async def test_process_source_shape_rejected(self):
        """回退后进程形态一律 invalid_audio_source，且不改变当前源"""
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source',
            'source': {'kind': 'process', 'pid': 1234, 'name': 'chrome.exe'}
        })
        errors = [m for m in self.sent(app) if m.get('type') == 'error']
        assert len(errors) == 1
        assert errors[0]['code'] == 'invalid_audio_source'
        assert app.audio_capture.get_current_source() == {'kind': 'device', 'id': ''}

    async def test_missing_source_is_invalid(self):
        """缺少 source/source_id → invalid_audio_source"""
        app = self.make_app()
        await app._on_control_message({
            'type': 'control', 'action': 'set_audio_source'
        })
        errors = [m for m in self.sent(app) if m.get('type') == 'error']
        assert len(errors) == 1
        assert errors[0]['code'] == 'invalid_audio_source'


class TestChangeLlmControl:
    """change_llm 控制分发：成功广播设备状态、失败回执"""

    def make_app(self):
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        app.websocket_server.send = AsyncMock()
        return app

    @staticmethod
    def sent(app):
        return [c.args[0] for c in app.websocket_server.send.call_args_list]

    async def test_change_llm_success_broadcasts_device_state(self):
        app = self.make_app()
        app.translator = AsyncMock()
        app.translator.resolved_device = 'cpu'
        app.translator.device_reason = 'no_cuda'

        await app._on_control_message({
            'type': 'control', 'action': 'change_llm',
            'model_id': 'qwen3-1.7b-q4km',
        })

        app.translator.change_llm.assert_awaited_once_with('qwen3-1.7b-q4km')
        msgs = self.sent(app)
        assert not [m for m in msgs if m.get('type') == 'error']
        states = [m for m in msgs if m.get('type') == 'device_state']
        assert states
        assert states[0]['translation'] == {'resolved': 'cpu', 'reason': 'no_cuda'}

    async def test_change_llm_invalid_receipt(self):
        app = self.make_app()
        app.translator.change_llm = AsyncMock(side_effect=ValueError('不支持'))

        await app._on_control_message({
            'type': 'control', 'action': 'change_llm', 'model_id': 'nope',
        })

        msgs = self.sent(app)
        errors = [m for m in msgs if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'invalid_llm'
        assert not [m for m in msgs if m.get('type') == 'device_state']

    async def test_change_llm_download_failure_receipt(self):
        app = self.make_app()
        app.translator.change_llm = AsyncMock(
            side_effect=DownloadError('all sources failed')
        )

        await app._on_control_message({
            'type': 'control', 'action': 'change_llm', 'model_id': 'hy-mt2-7b-q4km',
        })

        errors = [m for m in self.sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'model_download_failed'

    async def test_change_llm_load_failure_receipt(self):
        app = self.make_app()
        app.translator.change_llm = AsyncMock(side_effect=RuntimeError('spawn failed'))

        await app._on_control_message({
            'type': 'control', 'action': 'change_llm', 'model_id': 'hy-mt2-7b-q4km',
        })

        errors = [m for m in self.sent(app) if m.get('type') == 'error']
        assert errors and errors[0]['code'] == 'llm_load_failed'


class TestBroadcastSnapshot:
    """广播遍历快照：发送期间客户端集合被修改不得抛异常（fix-asr-runtime-stall）"""

    async def test_send_tolerates_client_set_mutation(self):
        server = WebSocketServer(
            {'websocket': {'host': 'localhost', 'port': TEST_PORT}}
        )

        class _MutatingClient:
            async def send(self, message):
                server._clients.discard(self)  # 发送中断开自身

        client = _MutatingClient()
        server._clients.add(client)

        await server.send({'type': 'x'})  # MUST NOT raise（旧实现抛 Set changed size）
        assert client not in server._clients

    async def test_send_reaches_remaining_clients(self):
        server = WebSocketServer(
            {'websocket': {'host': 'localhost', 'port': TEST_PORT}}
        )

        class _MutatingClient:
            async def send(self, message):
                server._clients.discard(self)

        class _RecordingClient:
            def __init__(self):
                self.received = []

            async def send(self, message):
                self.received.append(message)

        mutating = _MutatingClient()
        recording = _RecordingClient()
        server._clients.update({mutating, recording})

        await server.send({'type': 'y'})

        assert len(recording.received) == 1
        assert json.loads(recording.received[0]) == {'type': 'y'}


class TestDevicePreference:
    """SUBTITLE_DEVICE > config.yaml > auto，且作用于两引擎"""

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv('SUBTITLE_DEVICE', 'cpu')
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        assert app.device_preference == 'cpu'
        assert app.asr_engine.device == 'cpu'
        assert app.translator.device == 'cpu'

    def test_env_cuda_applies_to_both(self, monkeypatch):
        monkeypatch.setenv('SUBTITLE_DEVICE', 'cuda')
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        assert app.asr_engine.device == 'cuda'
        assert app.translator.device == 'cuda'

    def test_env_auto_falls_back_to_config(self, monkeypatch):
        monkeypatch.setenv('SUBTITLE_DEVICE', 'auto')
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        assert app.device_preference == 'auto'

    def test_invalid_env_ignored(self, monkeypatch):
        monkeypatch.setenv('SUBTITLE_DEVICE', 'tpu')
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        assert app.device_preference == 'auto'
