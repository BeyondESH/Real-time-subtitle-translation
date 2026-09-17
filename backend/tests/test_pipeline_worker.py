"""
PipelineWorker 背压与消费测试
"""
import asyncio
from unittest.mock import AsyncMock

import numpy as np
import pytest

from pipeline_worker import PipelineWorker


def make_worker(queue_size=2, transcribe_result=None, slow=0):
    asr = AsyncMock()
    asr.is_ready = True

    async def transcribe(audio):
        if slow:
            await asyncio.sleep(slow)
        return transcribe_result

    asr.transcribe = transcribe
    translator = AsyncMock()
    translator.translate = AsyncMock(return_value={'zh': '你好'})
    ws = AsyncMock()
    ws.send = AsyncMock()
    worker = PipelineWorker(asr, translator, ws, queue_size=queue_size)
    return worker, asr, translator, ws


def audio(seconds=1.0):
    return np.zeros(int(16000 * seconds), dtype=np.float32)


class TestBackpressure:
    async def test_queue_full_drops_oldest_and_warns(self):
        """队满丢最旧 + 广播 pipeline_warning"""
        worker, asr, translator, ws = make_worker(queue_size=2)
        await worker.start()
        try:
            # 停掉消费循环，只积累队列（模拟 ASR 卡住）
            worker._task.cancel()
            try:
                await worker._task
            except asyncio.CancelledError:
                pass

            assert worker.submit(audio()) is True
            assert worker.submit(audio()) is True
            assert worker.submit(audio()) is False  # 触发丢弃
            assert worker.submit(audio()) is False

            await asyncio.sleep(0.05)  # 让告警协程执行

            assert worker.dropped_count == 2
            assert worker.pending_count == 2
            warning_msgs = [
                c.args[0] for c in ws.send.call_args_list
                if c.args[0].get('type') == 'pipeline_warning'
            ]
            assert len(warning_msgs) == 2
        finally:
            await worker.stop()

    async def test_consume_after_recovery(self):
        """负载恢复后正常消费"""
        result = {'text': 'こんにちは', 'language': 'ja'}
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=result)
        await worker.start()
        try:
            worker.submit(audio())
            worker.submit(audio())
            # 等待两条字幕都广播出去
            for _ in range(100):
                n = sum(
                    1 for c in ws.send.call_args_list
                    if c.args[0].get('type') == 'subtitle'
                )
                if n == 2:
                    break
                await asyncio.sleep(0.01)

            subtitle_msgs = [
                c.args[0] for c in ws.send.call_args_list
                if c.args[0].get('type') == 'subtitle'
            ]
            assert len(subtitle_msgs) == 2
            assert subtitle_msgs[0]['original'] == 'こんにちは'
            assert subtitle_msgs[0]['source_language'] == 'ja'
            assert subtitle_msgs[0]['translations'] == {'zh': '你好'}
        finally:
            await worker.stop()

    async def test_drop_when_asr_not_ready(self):
        """ASR 切换中（is_ready=False）语句被丢弃"""
        result = {'text': 'hi', 'language': 'en'}
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=result)
        asr.is_ready = False
        await worker.start()
        try:
            worker.submit(audio())
            for _ in range(100):
                if worker.dropped_count == 1:
                    break
                await asyncio.sleep(0.01)

            assert worker.dropped_count == 1
            assert not any(
                c.args[0].get('type') == 'subtitle' for c in ws.send.call_args_list
            )
        finally:
            await worker.stop()

    async def test_submit_before_start(self):
        """未启动时 submit 不抛异常"""
        worker, *_ = make_worker()
        assert worker.submit(audio()) is False

    async def test_empty_transcription_not_sent(self):
        """空识别结果不广播字幕"""
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=None)
        await worker.start()
        try:
            worker.submit(audio())
            for _ in range(100):
                if worker.pending_count == 0:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # 等消费协程走完
            assert not any(
                c.args[0].get('type') == 'subtitle' for c in ws.send.call_args_list
            )
        finally:
            await worker.stop()


class TestTimestamps:
    """subtitle 消息时间戳透传（pipeline-control spec）"""

    async def _wait_subtitle(self, worker, ws):
        for _ in range(100):
            if any(
                c.args[0].get('type') == 'subtitle' for c in ws.send.call_args_list
            ):
                break
            await asyncio.sleep(0.01)
        return [
            c.args[0] for c in ws.send.call_args_list
            if c.args[0].get('type') == 'subtitle'
        ][0]

    async def test_segment_timestamps_in_subtitle(self):
        """UtteranceSegment 提交 → 消息含 ts_start/ts_end"""
        from audio_buffer import UtteranceSegment
        result = {'text': 'hi', 'language': 'en'}
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=result)
        await worker.start()
        try:
            worker.submit(UtteranceSegment(audio(1.0), ts_start=1.5, ts_end=2.75))
            msg = await self._wait_subtitle(worker, ws)
            assert msg['ts_start'] == 1.5
            assert msg['ts_end'] == 2.75
        finally:
            await worker.stop()

    async def test_raw_array_submit_message_shape_unchanged(self):
        """裸数组兼容路径：消息不含 ts 字段，既有字段语义不变（旧客户端兼容）"""
        result = {'text': 'hi', 'language': 'en'}
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=result)
        await worker.start()
        try:
            worker.submit(audio(1.0))
            msg = await self._wait_subtitle(worker, ws)
            assert 'ts_start' not in msg
            assert 'ts_end' not in msg
            assert msg['original'] == 'hi'
            assert msg['source_language'] == 'en'
            assert msg['translations'] == {'zh': '你好'}
        finally:
            await worker.stop()
