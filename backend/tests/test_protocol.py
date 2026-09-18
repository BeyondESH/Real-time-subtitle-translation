"""
WebSocket 请求/响应协议与激活语言控制测试
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
import websockets

import asr_engine as asr_mod
import translator as tr_mod
from asr_engine import ASREngine
from main import SubtitleTranslator
from pipeline_worker import PipelineWorker
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
        translator.translate = AsyncMock(return_value={'en': 'hello'})
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
        translator.translate.assert_called_once_with('こんにちは', 'ja', ['en'])
        msg = [c.args[0] for c in ws.send.call_args_list
               if c.args[0].get('type') == 'subtitle'][0]
        assert msg['active_language'] == 'en'
        assert msg['translations'] == {'en': 'hello'}


def _probe(cuda_available: bool, detail: str = 'test-probe') -> dict:
    return {'cuda_available': cuda_available, 'source': 'fake', 'detail': detail}


class TestASREngineDevice:
    """ASR 统一加载路径 + change_device（假加载器，无网络）"""

    def make_engine(self, monkeypatch, cuda_available, fail_cuda=False,
                    device='auto'):
        created = []

        def fake_model(model_size, device, compute_type, download_root):
            created.append((device, compute_type))
            if device == 'cuda' and fail_cuda:
                raise RuntimeError(
                    'CUDA failed with error no CUDA-capable device is detected'
                )
            return object()

        monkeypatch.setattr(asr_mod, 'WhisperModel', fake_model)
        monkeypatch.setattr(
            asr_mod, 'probe_compute', lambda: {'asr': _probe(cuda_available)}
        )
        engine = ASREngine({
            'asr': {'model_size': 'tiny', 'device': device, 'compute_type': 'float16'}
        })
        return engine, created

    async def test_auto_with_cuda_uses_gpu(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=True)
        await engine.initialize()
        assert engine.resolved_device == 'cuda'
        assert engine.device_reason == 'auto'
        assert created == [('cuda', 'float16')]

    async def test_auto_without_cuda_uses_cpu(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=False)
        await engine.initialize()
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'no_cuda'
        assert created == [('cpu', 'int8')]

    async def test_explicit_cuda_probe_unavailable_skips_gpu(self, monkeypatch):
        engine, created = self.make_engine(
            monkeypatch, cuda_available=False, device='cuda'
        )
        await engine.initialize()
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'no_cuda'
        assert created == [('cpu', 'int8')]  # 未尝试注定失败的 GPU 加载

    async def test_explicit_cpu_never_tries_gpu(self, monkeypatch):
        engine, created = self.make_engine(
            monkeypatch, cuda_available=True, device='cpu'
        )
        await engine.initialize()
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'user'
        assert created == [('cpu', 'int8')]

    @pytest.mark.parametrize('device', ['auto', 'cuda'])
    async def test_gpu_load_failure_silent_degrade(self, monkeypatch, caplog, device):
        engine, created = self.make_engine(
            monkeypatch, cuda_available=True, fail_cuda=True, device=device
        )
        with caplog.at_level(logging.WARNING, logger='asr_engine'):
            await engine.initialize()  # MUST NOT raise

        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'load_failed'
        assert created == [('cuda', 'float16'), ('cpu', 'int8')]
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
        assert created == [('cpu', 'int8'), ('cpu', 'int8')]

    async def test_change_device_to_cuda_without_probe_degrades(self, monkeypatch):
        engine, created = self.make_engine(monkeypatch, cuda_available=False)
        await engine.initialize()

        await engine.change_device('cuda')
        assert engine.device == 'cuda'
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'no_cuda'
        # 未新增 GPU 尝试
        assert ('cuda', 'float16') not in created

    def test_get_model_info_keeps_preference_and_adds_resolved(self, monkeypatch):
        engine, _ = self.make_engine(monkeypatch, cuda_available=True)
        info = engine.get_model_info()
        assert info['device'] == 'auto'  # 配置偏好语义不变
        assert info['resolved_device'] is None
        assert info['device_reason'] is None
        assert 'resolved_device' in info and 'device_reason' in info


class TestTranslatorDevice:
    """翻译设备解析 + change_device（假探针/假加载，无网络）"""

    def make_translator(self, monkeypatch, cuda_available):
        monkeypatch.setattr(
            tr_mod, 'probe_compute',
            lambda: {'translation': _probe(cuda_available)},
        )
        return Translator({
            'translation': {
                'device': 'auto', 'lazy_load': True, 'preload_primary': True
            }
        })

    async def test_initialize_resolves_cpu_no_cuda(self, monkeypatch):
        tr = self.make_translator(monkeypatch, cuda_available=False)
        await tr.initialize()
        assert tr.device == 'auto'  # 偏好保持
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'no_cuda'

    async def test_initialize_resolves_cuda_auto(self, monkeypatch):
        tr = self.make_translator(monkeypatch, cuda_available=True)
        await tr.initialize()
        assert tr.resolved_device == 'cuda'
        assert tr.device_reason == 'auto'

    async def test_get_model_info_adds_fields(self, monkeypatch):
        tr = self.make_translator(monkeypatch, cuda_available=False)
        info = tr.get_model_info()
        assert info['device'] == 'auto'
        assert info['resolved_device'] is None
        assert info['device_reason'] is None

    async def test_change_device_invalid_raises(self, monkeypatch):
        tr = self.make_translator(monkeypatch, cuda_available=False)
        with pytest.raises(ValueError):
            await tr.change_device('tpu')

    async def test_change_device_same_value_idempotent(self, monkeypatch):
        tr = self.make_translator(monkeypatch, cuda_available=False)
        await tr.initialize()
        tr._primary_model = object()  # 已加载
        await tr.change_device('auto')
        assert tr._primary_model is not None  # 未卸载

    async def test_change_device_resets_and_reloads_primary(self, monkeypatch):
        tr = self.make_translator(monkeypatch, cuda_available=False)
        await tr.initialize()
        tr._primary_model = object()
        tr._primary_tokenizer = object()
        tr._fallback_model = object()
        tr._fallback_tokenizer = object()

        reloaded = []

        async def fake_ensure_primary():
            reloaded.append(tr.resolved_device)
            tr._primary_model = object()

        tr.ensure_primary = fake_ensure_primary

        await tr.change_device('cuda')  # 探针不可用 → cpu/no_cuda

        assert tr.device == 'cuda'
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'no_cuda'
        assert tr._primary_model is None  # 已卸载
        assert tr._fallback_model is None  # NLLB 一并卸载

        for _ in range(10):
            if reloaded:
                break
            await asyncio.sleep(0)
        assert reloaded == ['cpu']  # 后台按新设备重新预载

    async def test_change_device_unloaded_primary_not_reloaded(self, monkeypatch):
        tr = self.make_translator(monkeypatch, cuda_available=False)
        await tr.initialize()
        calls = []

        async def fake_ensure_primary():
            calls.append(1)

        tr.ensure_primary = fake_ensure_primary
        await tr.change_device('cuda')

        for _ in range(5):
            await asyncio.sleep(0)
        assert calls == []  # 切换前未加载 → 不后台预载


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

    async def test_broadcast_with_no_clients_is_safe(self):
        """真实 WebSocketServer 无客户端时广播 MUST NOT 抛异常"""
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        await app._broadcast_device_state()
        assert app.websocket_server.get_client_count() == 0

    async def test_preload_primary_broadcasts_device_state(self):
        app = self.make_app()
        app.translator.ensure_primary = AsyncMock()
        app.translator._resolved_device = 'cpu'
        app.translator._device_reason = 'no_cuda'

        await app._preload_primary_model()

        types = [m.get('type') for m in self.sent(app)]
        assert 'device_state' in types

    async def test_preload_failure_still_broadcasts_and_does_not_raise(self):
        app = self.make_app()
        app.translator.ensure_primary = AsyncMock(side_effect=RuntimeError('net down'))

        await app._preload_primary_model()  # MUST NOT raise

        types = [m.get('type') for m in self.sent(app)]
        assert 'device_state' in types

    async def test_start_broadcasts_after_asr_initialize(self):
        app = self.make_app()
        app.websocket_server.start = AsyncMock()
        app.asr_engine.initialize = AsyncMock()
        app.asr_engine._resolved_device = 'cpu'
        app.asr_engine._device_reason = 'no_cuda'
        app.translator.initialize = AsyncMock()
        app.translator._resolved_device = 'cpu'
        app.translator._device_reason = 'no_cuda'
        app.config['translation']['preload_primary'] = False
        app.worker.start = AsyncMock()
        app.audio_capture.start = AsyncMock()
        app._segmentation_loop = AsyncMock()

        await app.start()
        try:
            states = [m for m in self.sent(app) if m.get('type') == 'device_state']
            assert len(states) == 1
            assert states[0]['asr'] == {'resolved': 'cpu', 'reason': 'no_cuda'}
        finally:
            await app.stop()


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
