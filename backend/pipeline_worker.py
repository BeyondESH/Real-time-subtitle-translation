"""
管线工作器：有界语句队列 + ASR + 翻译 + WebSocket 广播

背压策略：队列满时丢弃最旧语句并向客户端广播 pipeline_warning，
绝不为语句创建无界并发任务。
"""
import asyncio
import logging
from typing import Optional, Union

import numpy as np

from audio_buffer import UtteranceSegment

logger = logging.getLogger(__name__)


class PipelineWorker:
    """语句消费协程：ASR → 翻译（仅激活语言）→ 广播"""

    def __init__(self, asr_engine, translator, ws_server, queue_size: int = 8,
                 get_active_language=None):
        if queue_size <= 0:
            raise ValueError(f"队列深度必须为正: {queue_size}")
        self._asr = asr_engine
        self._translator = translator
        self._ws = ws_server
        self._get_active_language = get_active_language  # () -> str，可空
        self._queue_size = queue_size
        self._queue: Optional[asyncio.Queue] = None  # start() 中创建（绑定运行中的事件循环）
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    @property
    def pending_count(self) -> int:
        return self._queue.qsize() if self._queue is not None else 0

    def submit(self, segment: Union[UtteranceSegment, np.ndarray]) -> bool:
        """
        提交语句段（事件循环线程，非阻塞）。

        接受 UtteranceSegment（含时间戳）或裸 float32 数组（兼容路径，
        时间戳缺失，广播不含 ts 字段）。

        队列满时丢弃最旧语句、记录日志并广播 pipeline_warning。

        Returns:
            True 正常入队；False 发生了丢弃
        """
        if self._queue is None:
            logger.warning("PipelineWorker 未启动，语句被丢弃")
            return False

        if isinstance(segment, np.ndarray):
            segment = UtteranceSegment(audio=segment)

        try:
            self._queue.put_nowait(segment)
            return True
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dropped_count += 1
            self._queue.put_nowait(segment)
            logger.warning(f"语句队列已满，丢弃最旧语句（累计丢弃 {self._dropped_count}）")
            asyncio.create_task(self._ws.send({
                'type': 'pipeline_warning',
                'reason': 'queue_full',
                'dropped': self._dropped_count,
                'message': '处理速度跟不上音频输入，已丢弃最旧的语句'
            }))
            return False

    async def start(self):
        """启动消费协程"""
        if self._running:
            return
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info(f"PipelineWorker 已启动（队列深度 {self._queue_size}）")

    async def stop(self):
        """停止消费协程"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("PipelineWorker 已停止")

    async def _run(self):
        """消费循环"""
        while self._running:
            try:
                segment = await self._queue.get()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"队列读取错误: {e}")
                continue

            try:
                await self._process(segment)
            except Exception as e:
                logger.error(f"语句处理失败: {e}")

    async def _process(self, segment: UtteranceSegment):
        """处理单条语句段：ASR → 翻译 → 广播（含时间戳透传）"""
        # ASR 未就绪（如模型切换中）时丢弃
        if not getattr(self._asr, 'is_ready', True):
            self._dropped_count += 1
            logger.debug("ASR 未就绪，语句被丢弃")
            return

        transcription = await self._asr.transcribe(segment.audio)
        if not transcription or not transcription.get('text'):
            return

        # 只翻译激活目标语言（多语言全翻会放大推理延迟）
        active = self._get_active_language() if self._get_active_language else None
        targets = [active] if active else None
        translations = await self._translator.translate(
            transcription['text'],
            transcription['language'],
            targets
        )

        payload = {
            'type': 'subtitle',
            'original': transcription['text'],
            'source_language': transcription['language'],
            'active_language': active,
            'translations': translations
        }
        # 时间戳透传（SRT 精确导出的前提）；兼容路径缺失时不附加字段
        if segment.ts_start is not None and segment.ts_end is not None:
            payload['ts_start'] = round(segment.ts_start, 3)
            payload['ts_end'] = round(segment.ts_end, 3)

        await self._ws.send(payload)
