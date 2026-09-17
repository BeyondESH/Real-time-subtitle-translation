"""
模型下载进度回调线程安全测试（回归 D3：worker 线程 create_task 崩溃 bug）
"""
import asyncio
import threading
from unittest.mock import AsyncMock

import pytest

from main import SubtitleTranslator


def make_app() -> SubtitleTranslator:
    return SubtitleTranslator(config_path='nonexistent-config.yaml')


class TestProgressCallback:
    def test_no_loop_no_crash(self):
        """无事件循环时回调不抛异常（仅记日志）"""
        app = make_app()
        errors = []

        def trigger():
            try:
                app._on_model_progress('model', 50.0, '下载中')
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=trigger)
        t.start()
        t.join()
        assert not errors

    async def test_worker_thread_hop_delivers(self):
        """worker 线程触发回调 → 经 call_soon_threadsafe 送达前端"""
        app = make_app()
        app.websocket_server.send = AsyncMock()
        app._loop = asyncio.get_running_loop()

        def trigger():
            # 模拟模型加载 worker 线程（无 running loop）
            app._on_model_progress('nllb', 99.0, '即将完成')

        t = threading.Thread(target=trigger)
        t.start()
        try:
            for _ in range(200):
                if app.websocket_server.send.called:
                    break
                await asyncio.sleep(0.01)
        finally:
            t.join()

        app.websocket_server.send.assert_called_once()
        msg = app.websocket_server.send.call_args.args[0]
        assert msg['type'] == 'model_progress'
        assert msg['model_name'] == 'nllb'
        assert msg['progress'] == 99.0

    async def test_in_loop_trigger_delivers(self):
        """事件循环线程内触发同样送达"""
        app = make_app()
        app.websocket_server.send = AsyncMock()
        app._loop = asyncio.get_running_loop()

        app._on_model_progress('primary', 100.0, '完成')
        for _ in range(200):
            if app.websocket_server.send.called:
                break
            await asyncio.sleep(0.01)

        app.websocket_server.send.assert_called_once()
