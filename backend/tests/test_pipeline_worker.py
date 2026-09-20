"""
PipelineWorker 背压与消费测试
"""
import asyncio
import logging
import time
from unittest.mock import AsyncMock

import numpy as np
import pytest

from audio_buffer import UtteranceSegment
from pipeline_worker import PipelineWorker


def make_worker(queue_size=2, transcribe_result=None, slow=0,
                active=None, metrics=None):
    asr = AsyncMock()
    asr.is_ready = True

    async def transcribe(audio):
        if slow:
            await asyncio.sleep(slow)
        return transcribe_result

    asr.transcribe = transcribe
    translator = AsyncMock()
    translator.translate_with_metrics = AsyncMock(
        return_value=({'zh': '你好'}, {'zh': 42.5} if metrics is None else metrics)
    )
    ws = AsyncMock()
    ws.send = AsyncMock()
    worker = PipelineWorker(
        asr, translator, ws, queue_size=queue_size,
        get_active_language=(lambda: active) if active else None
    )
    return worker, asr, translator, ws


def audio(seconds=1.0):
    return np.zeros(int(16000 * seconds), dtype=np.float32)


def make_streaming_worker(partials=('你好',), final_text='你好', active='zh',
                          queue_size=4, transcribe_result=None,
                          raise_after_partial=False, sleep_after_partial=0):
    """构造带流式回调的 worker：翻译双打器逐块回调 on_partial 后返回定稿。"""
    asr = AsyncMock()
    asr.is_ready = True
    result = transcribe_result if transcribe_result is not None else {
        'text': 'こんにちは', 'language': 'ja'
    }

    async def transcribe(audio_data):
        return result

    asr.transcribe = transcribe

    translator = AsyncMock()
    translator.is_ready = True

    async def translate_with_metrics(text, source_language=None, targets=None,
                                     on_partial=None):
        lang = targets[0] if targets else 'zh'
        for i, chunk in enumerate(partials):
            if on_partial is not None:
                await on_partial(lang, chunk, i == 0)
        if raise_after_partial:
            raise RuntimeError('翻译请求失败')
        if sleep_after_partial:
            await asyncio.sleep(sleep_after_partial)
        return ({lang: final_text}, {lang: 42.5})

    translator.translate_with_metrics = translate_with_metrics
    ws = AsyncMock()
    ws.send = AsyncMock()
    worker = PipelineWorker(
        asr, translator, ws, queue_size=queue_size,
        get_active_language=(lambda: active) if active else None,
    )
    return worker, ws


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


class TestGenerateSpeed:
    """subtitle 消息生成速度透传（pipeline-control spec：tps）"""

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

    async def test_tps_attached_when_active_translated(self):
        """激活语言翻译成功 → 消息携带 tps"""
        result = {'text': 'こんにちは', 'language': 'ja'}
        worker, asr, translator, ws = make_worker(
            queue_size=4, transcribe_result=result, active='zh', metrics={'zh': 46.47}
        )
        await worker.start()
        try:
            worker.submit(audio(1.0))
            msg = await self._wait_subtitle(worker, ws)
            assert msg['active_language'] == 'zh'
            assert msg['tps'] == pytest.approx(46.47)
        finally:
            await worker.stop()

    async def test_tps_omitted_when_metric_missing(self):
        """翻译失败/指标缺失 → 消息不含 tps 字段"""
        result = {'text': 'こんにちは', 'language': 'ja'}
        worker, asr, translator, ws = make_worker(
            queue_size=4, transcribe_result=result, active='zh', metrics={}
        )
        await worker.start()
        try:
            worker.submit(audio(1.0))
            msg = await self._wait_subtitle(worker, ws)
            assert 'tps' not in msg
        finally:
            await worker.stop()

    async def test_tps_omitted_without_active_language(self):
        """无激活语言（兼容路径）→ 即使有指标也不附加字段"""
        result = {'text': 'こんにちは', 'language': 'ja'}
        worker, asr, translator, ws = make_worker(
            queue_size=4, transcribe_result=result, metrics={'zh': 46.47}
        )
        await worker.start()
        try:
            worker.submit(audio(1.0))
            msg = await self._wait_subtitle(worker, ws)
            assert 'tps' not in msg
        finally:
            await worker.stop()


async def wait_until(cond, timeout=2.0):
    """轮询等待条件成立"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


class TestStallSupervision:
    """停滞监管与整句预算（fix-asr-runtime-stall）"""

    async def test_stall_condition_inflight_within_budget_not_stalled(self):
        """在途语句在预算内 = 慢而非停滞（慢而非死不误报）"""
        worker, *_ = make_worker()
        worker._queue = asyncio.Queue(maxsize=8)
        worker._queue.put_nowait(UtteranceSegment(audio=audio()))
        worker._inflight_segment = UtteranceSegment(audio=audio(1.0))
        worker._inflight_started = time.monotonic()
        worker._last_progress = time.monotonic() - 1000
        assert worker._stall_deadline_exceeded() is False

    async def test_stall_condition_idle_exceeds_threshold(self):
        """无在途且超阈值无进度 = 停滞"""
        worker, *_ = make_worker()
        worker._queue = asyncio.Queue(maxsize=8)
        worker._queue.put_nowait(UtteranceSegment(audio=audio()))
        worker._inflight_segment = None
        worker._inflight_started = None
        worker._last_progress = time.monotonic() - 1000
        assert worker._stall_deadline_exceeded() is True

    async def test_stall_condition_empty_queue_not_stalled(self):
        """无待处理不判停滞"""
        worker, *_ = make_worker()
        worker._queue = asyncio.Queue(maxsize=8)
        worker._last_progress = time.monotonic() - 1000
        assert worker._stall_deadline_exceeded() is False

    async def test_handle_stall_purges_counts_warns_and_recovers(self):
        """停滞处置：告警（检测）→ 清积压计数 → 健康回调 → 告警（恢复）"""
        calls = []
        worker, asr, translator, ws = make_worker(queue_size=4)
        worker._on_stall = lambda: calls.append(1)
        await worker.start()
        try:
            # 模拟消费者卡死：停掉消费任务
            worker._task.cancel()
            try:
                await worker._task
            except asyncio.CancelledError:
                pass

            for _ in range(3):
                worker.submit(audio())

            await worker._handle_stall()

            assert worker.dropped_count == 3
            assert worker.pending_count == 0
            assert calls == [1]

            warnings = [
                c.args[0] for c in ws.send.call_args_list
                if c.args[0].get('type') == 'pipeline_warning'
            ]
            assert [w['reason'] for w in warnings] == ['stalled', 'stalled']
            assert warnings[0]['pending'] == 3
            assert '正在自动恢复' in warnings[0]['message']
            assert '已清理积压 3 句' in warnings[1]['message']
            assert all('dropped' in w for w in warnings)
        finally:
            await worker.stop()

    async def test_segment_budget_timeout_drops_and_continues(self, monkeypatch):
        """整句预算超时：丢弃该句并继续消费（活性兜底）"""
        import pipeline_worker as pw
        monkeypatch.setattr(pw, 'segment_budget_s', lambda segment: 0.05)

        state = {'slow': True}
        asr = AsyncMock()
        asr.is_ready = True

        async def transcribe(audio_data):
            if state['slow']:
                await asyncio.sleep(5)  # 超过预算 → wait_for 取消
            return {'text': 'hi', 'language': 'en'}

        asr.transcribe = transcribe
        translator = AsyncMock()
        translator.translate_with_metrics = AsyncMock(
            return_value=({'zh': '好'}, {'zh': 42.5})
        )
        ws = AsyncMock()
        ws.send = AsyncMock()
        worker = PipelineWorker(asr, translator, ws, queue_size=4)

        await worker.start()
        try:
            worker.submit(audio())
            assert await wait_until(lambda: worker.dropped_count >= 1)

            state['slow'] = False
            worker.submit(audio())
            assert await wait_until(lambda: any(
                c.args[0].get('type') == 'subtitle' for c in ws.send.call_args_list
            ))
        finally:
            await worker.stop()

    async def test_queue_full_warning_payload_shape(self):
        """queue_full 告警保留既有字段与语义（旧客户端兼容）"""
        worker, asr, translator, ws = make_worker(queue_size=1)
        await worker.start()
        try:
            worker._task.cancel()
            try:
                await worker._task
            except asyncio.CancelledError:
                pass

            assert worker.submit(audio()) is True
            assert worker.submit(audio()) is False
            assert await wait_until(lambda: any(
                c.args[0].get('type') == 'pipeline_warning'
                for c in ws.send.call_args_list
            ))
            warning = [
                c.args[0] for c in ws.send.call_args_list
                if c.args[0].get('type') == 'pipeline_warning'
            ][-1]
            assert warning['reason'] == 'queue_full'
            assert warning['dropped'] == 1
            assert 'message' in warning
        finally:
            await worker.stop()


class TestLatencyMetrics:
    """subtitle 消息分阶段耗时透传（pipeline-control spec：latency）"""

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

    async def test_latency_attached_with_full_segment(self, caplog):
        """完整切句字段 → latency 5 键齐全（非负整数 ms），并输出分解日志"""
        caplog.set_level(logging.INFO)
        result = {'text': 'hi', 'language': 'en'}
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=result)
        await worker.start()
        try:
            now = time.monotonic()
            worker.submit(UtteranceSegment(
                audio(1.0), ts_start=0.0, ts_end=1.0,
                cut_at=now - 1.0, silence_wait_s=0.8,
            ))
            msg = await self._wait_subtitle(worker, ws)
            latency = msg['latency']
            assert set(latency) == {
                'endpoint_ms', 'queue_ms', 'asr_ms', 'llm_ms', 'total_ms'
            }
            assert all(isinstance(v, int) and v >= 0 for v in latency.values())
            assert latency['endpoint_ms'] == 800
            # total_ms（切句→广播）覆盖队列+识别+翻译之和（毫秒舍入容差）
            assert latency['total_ms'] >= (
                latency['queue_ms'] + latency['asr_ms'] + latency['llm_ms'] - 2
            )
            assert any('端到端' in r.getMessage() for r in caplog.records)
        finally:
            await worker.stop()

    async def test_latency_omitted_for_raw_array(self):
        """兼容路径（裸数组，无切句时刻）→ 不携带 latency"""
        result = {'text': 'hi', 'language': 'en'}
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=result)
        await worker.start()
        try:
            worker.submit(audio(1.0))
            msg = await self._wait_subtitle(worker, ws)
            assert 'latency' not in msg
        finally:
            await worker.stop()

    async def test_latency_omitted_when_silence_wait_missing(self):
        """全有或全无：切句时刻在但静音等待缺失 → 不携带 latency"""
        result = {'text': 'hi', 'language': 'en'}
        worker, asr, translator, ws = make_worker(queue_size=4, transcribe_result=result)
        await worker.start()
        try:
            worker.submit(UtteranceSegment(
                audio(1.0), ts_start=0.0, ts_end=1.0,
                cut_at=time.monotonic(), silence_wait_s=None,
            ))
            msg = await self._wait_subtitle(worker, ws)
            assert 'latency' not in msg
        finally:
            await worker.stop()


class TestStreamingLifecycle:
    """流式 partial 生命周期、id 贯穿与 first_token_ms（pipeline-control spec）"""

    @staticmethod
    async def _wait_type(ws, msg_type, timeout=2.0):
        """轮询等待指定类型广播并返回其消息列表"""
        found = await wait_until(lambda: any(
            c.args[0].get('type') == msg_type for c in ws.send.call_args_list
        ), timeout=timeout)
        assert found, f"未收到 {msg_type} 广播"
        return [
            c.args[0] for c in ws.send.call_args_list
            if c.args[0].get('type') == msg_type
        ]

    async def test_partial_shape_and_id_shared_with_final(self):
        """partial 形状精确；同句多帧与定稿共享同一 id"""
        worker, ws = make_streaming_worker(
            partials=('你好', '你好世界'), final_text='你好世界'
        )
        await worker.start()
        try:
            worker.submit(audio(1.0))
            partials = await self._wait_type(ws, 'subtitle_partial')
            finals = await self._wait_type(ws, 'subtitle')
            p, f = partials[-1], finals[-1]
            assert set(p) == {
                'type', 'id', 'original', 'source_language',
                'active_language', 'translations'
            }
            assert p['type'] == 'subtitle_partial'
            assert p['original'] == 'こんにちは'
            assert p['source_language'] == 'ja'
            assert p['active_language'] == 'zh'
            assert p['translations'] == {'zh': '你好世界'}
            assert p['id'] == 'u1'
            assert f['id'] == p['id']
            assert {x['id'] for x in partials} == {'u1'}
            assert f['translations'] == {'zh': '你好世界'}
        finally:
            await worker.stop()

    async def test_ids_monotonic_across_segments(self):
        """进程内 id 单调递增且不复用"""
        worker, ws = make_streaming_worker(partials=())
        await worker.start()
        try:
            worker.submit(audio())
            worker.submit(audio())
            assert await wait_until(lambda: sum(
                1 for c in ws.send.call_args_list
                if c.args[0].get('type') == 'subtitle'
            ) == 2)
            ids = [c.args[0]['id'] for c in ws.send.call_args_list
                   if c.args[0].get('type') == 'subtitle']
            assert ids == ['u1', 'u2']
        finally:
            await worker.stop()

    async def test_first_token_ms_present_for_streaming_segment(self):
        """流式且切句时刻已知 → 定稿携带非负整数 first_token_ms"""
        worker, ws = make_streaming_worker(partials=('你好',))
        await worker.start()
        try:
            worker.submit(UtteranceSegment(
                audio(1.0), ts_start=0.0, ts_end=1.0,
                cut_at=time.monotonic() - 0.5, silence_wait_s=0.4,
            ))
            final = (await self._wait_type(ws, 'subtitle'))[-1]
            assert isinstance(final['first_token_ms'], int)
            assert final['first_token_ms'] >= 0
        finally:
            await worker.stop()

    async def test_first_token_ms_absent_for_compat_path(self):
        """兼容路径（裸数组，无 cut_at）→ 即使有 partial 也不携带 field"""
        worker, ws = make_streaming_worker(partials=('你好',))
        await worker.start()
        try:
            worker.submit(audio(1.0))
            final = (await self._wait_type(ws, 'subtitle'))[-1]
            assert 'first_token_ms' not in final
        finally:
            await worker.stop()

    async def test_first_token_ms_absent_without_partial(self):
        """非流式（未产生 partial）→ 不携带 first_token_ms"""
        worker, ws = make_streaming_worker(partials=())
        await worker.start()
        try:
            worker.submit(UtteranceSegment(
                audio(1.0), ts_start=0.0, ts_end=1.0,
                cut_at=time.monotonic(), silence_wait_s=0.4,
            ))
            final = (await self._wait_type(ws, 'subtitle'))[-1]
            assert 'first_token_ms' not in final
        finally:
            await worker.stop()

    async def test_timeout_cancel_after_partial(self, monkeypatch):
        """预算超时取消：补发 subtitle_cancel（reason=timeout，同 id）"""
        import pipeline_worker as pw
        monkeypatch.setattr(pw, 'segment_budget_s', lambda segment: 0.05)
        worker, ws = make_streaming_worker(
            partials=('你好',), sleep_after_partial=10
        )
        await worker.start()
        try:
            worker.submit(audio(1.0))
            cancels = await self._wait_type(ws, 'subtitle_cancel')
            partials = await self._wait_type(ws, 'subtitle_partial')
            assert cancels[-1]['reason'] == 'timeout'
            assert cancels[-1]['id'] == partials[-1]['id']
            assert not any(
                c.args[0].get('type') == 'subtitle' for c in ws.send.call_args_list
            )
        finally:
            await worker.stop()

    async def test_failed_cancel_after_partial(self):
        """翻译异常：补发 subtitle_cancel（reason=failed，同 id）"""
        worker, ws = make_streaming_worker(
            partials=('你好',), raise_after_partial=True
        )
        await worker.start()
        try:
            worker.submit(audio(1.0))
            cancels = await self._wait_type(ws, 'subtitle_cancel')
            partials = await self._wait_type(ws, 'subtitle_partial')
            assert cancels[-1]['reason'] == 'failed'
            assert cancels[-1]['id'] == partials[-1]['id']
            assert not any(
                c.args[0].get('type') == 'subtitle' for c in ws.send.call_args_list
            )
        finally:
            await worker.stop()

    async def test_no_partial_emits_no_cancel_on_failure(self):
        """未推 partial 的失败路径 MUST NOT 产生清算帧"""
        worker, ws = make_streaming_worker(
            partials=(), raise_after_partial=True
        )
        await worker.start()
        try:
            worker.submit(audio(1.0))
            assert await wait_until(lambda: worker.pending_count == 0)
            await asyncio.sleep(0.05)
            types = {c.args[0].get('type') for c in ws.send.call_args_list}
            assert 'subtitle_cancel' not in types
            assert 'subtitle' not in types
        finally:
            await worker.stop()

    async def test_not_ready_paths_emit_no_partial_no_cancel(self):
        """引擎未就绪的早退路径：无 partial、无 final、无 cancel"""
        for attr in ('_asr', '_translator'):
            worker, ws = make_streaming_worker(partials=('你好',))
            getattr(worker, attr).is_ready = False
            await worker.start()
            try:
                worker.submit(audio(1.0))
                assert await wait_until(lambda w=worker: w.dropped_count == 1)
                await asyncio.sleep(0.05)
                types = {c.args[0].get('type') for c in ws.send.call_args_list}
                assert 'subtitle_partial' not in types
                assert 'subtitle_cancel' not in types
                assert 'subtitle' not in types
            finally:
                await worker.stop()
