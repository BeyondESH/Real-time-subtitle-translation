"""
集成测试 - 管线全链路（mock ASR/翻译模型）与控制消息
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from audio_buffer import RingBuffer, UtteranceSegmenter
from main import SubtitleTranslator
from pipeline_worker import PipelineWorker

SR = 16000


def sine(seconds: float) -> np.ndarray:
    t = np.arange(int(SR * seconds), dtype=np.float32) / SR
    return (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.float32)


class TestPipelineIntegration:
    """capture(模拟) → RingBuffer → Segmenter(stub VAD) → Worker → WS 全链路"""

    async def test_capture_to_subtitle_flow(self):
        buffer = RingBuffer(30 * SR)
        # 语音段 0.1s..2.1s，尾部留有 1s 静音 → 一次 tick 即完整切出
        vad_stub = lambda window: [{'start': 1600, 'end': 33600}]  # noqa: E731
        segmenter = UtteranceSegmenter(sample_rate=SR, vad_fn=vad_stub)

        asr = AsyncMock()
        asr.is_ready = True
        asr.transcribe = AsyncMock(return_value={'text': 'こんにちは', 'language': 'ja'})
        translator = AsyncMock()
        translator.translate = AsyncMock(return_value={'zh': '你好'})
        ws = AsyncMock()
        ws.send = AsyncMock()

        worker = PipelineWorker(asr, translator, ws, queue_size=4)
        await worker.start()
        try:
            # 模拟音频流入
            buffer.append(silence(0.1))
            buffer.append(sine(2.0))
            buffer.append(silence(1.0))

            # 切句 tick
            utterances = segmenter.tick(buffer)
            assert len(utterances) == 1
            for utt in utterances:
                assert worker.submit(utt) is True

            # 等待消费
            for _ in range(100):
                if worker.pending_count == 0:
                    break
                await asyncio.sleep(0.01)

            asr.transcribe.assert_called_once()
            # 源语言来自 ASR 结果（不经二次检测），targets=None 走全部配置目标
            translator.translate.assert_called_once_with('こんにちは', 'ja', None)

            subtitle_msgs = [
                c.args[0] for c in ws.send.call_args_list
                if c.args[0].get('type') == 'subtitle'
            ]
            assert len(subtitle_msgs) == 1
            assert subtitle_msgs[0]['original'] == 'こんにちは'
            assert subtitle_msgs[0]['translations'] == {'zh': '你好'}
            # 时间戳透传：语音段 1600..33600（头卷钳 0）→ 0.0..2.1s
            assert subtitle_msgs[0]['ts_start'] == 0.0
            assert subtitle_msgs[0]['ts_end'] == pytest.approx(2.1)
            assert subtitle_msgs[0]['ts_end'] > subtitle_msgs[0]['ts_start']
        finally:
            await worker.stop()


class TestControlMessages:
    """控制消息端到端语义"""

    def make_app(self):
        return SubtitleTranslator(config_path='nonexistent-config.yaml')

    async def test_pause_resume(self):
        app = self.make_app()
        app.audio_capture.pause = MagicMock()
        app.audio_capture.resume = MagicMock()

        await app._on_control_message({'type': 'control', 'action': 'pause'})
        app.audio_capture.pause.assert_called_once()

        await app._on_control_message({'type': 'control', 'action': 'resume'})
        app.audio_capture.resume.assert_called_once()

    async def test_change_model_invalid_receipt(self):
        app = self.make_app()
        app.asr_engine.change_model = AsyncMock(
            side_effect=ValueError("不支持的模型: xx")
        )
        app.websocket_server.send = AsyncMock()

        await app._on_control_message(
            {'type': 'control', 'action': 'change_model', 'model_size': 'xx'}
        )

        error_msgs = [
            c.args[0] for c in app.websocket_server.send.call_args_list
            if c.args[0].get('type') == 'error'
        ]
        assert len(error_msgs) == 1
        assert error_msgs[0]['code'] == 'invalid_model'

    async def test_unknown_action_ignored(self):
        app = self.make_app()
        # 不抛异常即通过
        await app._on_control_message({'type': 'control', 'action': 'explode'})
        await app._on_control_message({'type': 'mystery'})
        await app._on_control_message("not a dict")


class TestConfigLoading:
    def test_config_loading(self):
        import yaml
        config_path = os.path.join(
            os.path.dirname(__file__), '..', '..', 'config.yaml'
        )
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        assert 'audio' in config
        assert 'asr' in config
        assert 'translation' in config
        assert 'websocket' in config
        assert 'pipeline' in config
        assert 'vad' in config

    def test_default_config_complete(self):
        app = SubtitleTranslator(config_path='nonexistent-config.yaml')
        cfg = app._default_config()
        assert cfg['pipeline']['queue_size'] > 0
        assert cfg['vad']['min_silence_duration_ms'] > 0
        assert cfg['translation']['default_model'] == 'hy-mt2-1.8b-q4km'
