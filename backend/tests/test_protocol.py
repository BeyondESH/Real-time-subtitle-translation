"""
WebSocket 请求/响应协议与激活语言控制测试
"""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import websockets

from main import SubtitleTranslator
from pipeline_worker import PipelineWorker
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
