"""
管线工作器：有界语句队列 + ASR + 翻译 + WebSocket 广播

背压策略：队列满时丢弃最旧语句并向客户端广播 pipeline_warning（reason=queue_full），
绝不为语句创建无界并发任务。

停滞监管（fix-asr-runtime-stall）：进度心跳 + 独立监管任务；超过 stall_timeout_s
无处理进度时广播 stalled、清空过期积压（计入丢弃数）、触发引擎健康处置并继续消费，
恢复 MUST NOT 要求重启后端。单条语句受时间预算约束，超时丢弃并继续（活性兜底）。
"""
import asyncio
import logging
import time
from typing import Callable, Optional, Union

import numpy as np

from audio_buffer import UtteranceSegment

logger = logging.getLogger(__name__)

# 语音采样率（整句预算换算依赖；与管线 16kHz 契约一致）
_SAMPLE_RATE = 16000

# 停滞监管循环周期边界（秒）
_SUPERVISE_INTERVAL_MIN_S = 1.0
_SUPERVISE_INTERVAL_MAX_S = 5.0


def segment_budget_s(segment: UtteranceSegment) -> float:
    """
    单条语句的处理时间预算（秒）：覆盖 ASR 与翻译两侧。

    宽松系数容纳 CPU+int8 上"慢但活着"的推理；模块级函数便于测试 monkeypatch。
    """
    audio = getattr(segment, 'audio', None)
    duration = len(audio) / _SAMPLE_RATE if audio is not None else 0.0
    return max(30.0, 4.0 * duration + 20.0)


class PipelineWorker:
    """语句消费协程：ASR → 翻译（仅激活语言）→ 广播"""

    def __init__(self, asr_engine, translator, ws_server, queue_size: int = 8,
                 get_active_language=None, stall_timeout_s: float = 30.0,
                 on_stall: Optional[Callable[[], None]] = None):
        if queue_size <= 0:
            raise ValueError(f"队列深度必须为正: {queue_size}")
        if stall_timeout_s <= 0:
            raise ValueError(f"停滞阈值必须为正: {stall_timeout_s}")
        self._asr = asr_engine
        self._translator = translator
        self._ws = ws_server
        self._get_active_language = get_active_language  # () -> str，可空
        self._queue_size = queue_size
        self._queue: Optional[asyncio.Queue] = None  # start() 中创建（绑定运行中的事件循环）
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._dropped_count = 0

        # 停滞监管状态
        self._stall_timeout_s = float(stall_timeout_s)
        self._on_stall = on_stall  # 停滞健康处置回调（同步；异常不得外溢）
        self._last_progress = time.monotonic()
        self._inflight_segment: Optional[UtteranceSegment] = None
        self._inflight_started: Optional[float] = None
        self._supervise_task: Optional[asyncio.Task] = None

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

        队列满时丢弃最旧语句、记录日志并广播 pipeline_warning（reason=queue_full）。

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
        """启动消费协程与停滞监管任务"""
        if self._running:
            return
        self._queue = asyncio.Queue(maxsize=self._queue_size)
        self._running = True
        self._last_progress = time.monotonic()
        self._task = asyncio.create_task(self._run())
        self._supervise_task = asyncio.create_task(self._supervise())
        logger.info(
            "PipelineWorker 已启动（队列深度 %d，停滞阈值 %.0fs）",
            self._queue_size, self._stall_timeout_s,
        )

    async def stop(self):
        """停止消费协程与监管任务"""
        self._running = False
        await self._cancel_task(self._task)
        self._task = None
        await self._cancel_task(self._supervise_task)
        self._supervise_task = None
        logger.info("PipelineWorker 已停止")

    @staticmethod
    async def _cancel_task(task: Optional[asyncio.Task]):
        """取消任务并等待其退出（吞掉 CancelledError）"""
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self):
        """消费循环（每句受时间预算约束；超时丢弃并继续，保证活性）"""
        while self._running:
            try:
                segment = await self._queue.get()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"队列读取错误: {e}")
                continue

            # 出队即在途：更新进度心跳（在途起点计入"有进度"）
            self._inflight_segment = segment
            self._inflight_started = time.monotonic()
            self._last_progress = self._inflight_started
            try:
                await asyncio.wait_for(
                    self._process(segment), timeout=segment_budget_s(segment)
                )
            except asyncio.TimeoutError:
                self._dropped_count += 1
                logger.warning(
                    "语句处理超时（预算 %.0fs），丢弃该句并继续（累计丢弃 %d）",
                    segment_budget_s(segment), self._dropped_count,
                )
            except Exception as e:
                logger.error(f"语句处理失败: {e}")
            finally:
                self._inflight_segment = None
                self._inflight_started = None
                self._last_progress = time.monotonic()

    # ------------------------------------------------------------------ #
    # 停滞监管：检测 → 告警 → 清理积压 → 健康处置 → 恢复
    # ------------------------------------------------------------------ #

    def _stall_deadline_exceeded(self) -> bool:
        """
        停滞判定：队列有待处理且超过阈值无处理进度。

        在途语句在其时间预算内视为"慢"而非停滞（预算由消费循环的 wait_for 保证）；
        仅当在途超出"预算 + 停滞阈值"仍无进展时判定停滞（防御 wait_for 未生效的病理情况）。
        """
        if self.pending_count <= 0:
            return False
        now = time.monotonic()
        if self._inflight_segment is None:
            return (now - self._last_progress) >= self._stall_timeout_s
        return (now - self._inflight_started) >= (
            segment_budget_s(self._inflight_segment) + self._stall_timeout_s
        )

    async def _supervise(self):
        """停滞监管循环"""
        interval = max(
            _SUPERVISE_INTERVAL_MIN_S,
            min(_SUPERVISE_INTERVAL_MAX_S, self._stall_timeout_s / 4.0),
        )
        while self._running:
            await asyncio.sleep(interval)
            try:
                if self._stall_deadline_exceeded():
                    await self._handle_stall()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"停滞监管错误: {e}")

    async def _handle_stall(self):
        """停滞处置：告警（检测）→ 清空过期积压（计入丢弃）→ 健康处置 → 告警（恢复）"""
        pending = self.pending_count
        logger.warning(
            "检测到管线停滞（待处理 %d，%.0fs 无处理进度）",
            pending, self._stall_timeout_s,
        )
        await self._ws.send({
            'type': 'pipeline_warning',
            'reason': 'stalled',
            'dropped': self._dropped_count,
            'pending': pending,
            'message': '识别引擎停滞，正在自动恢复…'
        })

        # 清空过期积压（实时语义：不补播旧字幕）
        purged = 0
        while True:
            try:
                self._queue.get_nowait()
                purged += 1
            except asyncio.QueueEmpty:
                break
        self._dropped_count += purged
        self._last_progress = time.monotonic()

        # 触发引擎健康处置（如仍在 cuda 则一次性降级）
        if self._on_stall is not None:
            try:
                self._on_stall()
            except Exception:  # noqa: BLE001 - 回调异常不得影响监管
                logger.exception("停滞健康处置回调异常")

        await self._ws.send({
            'type': 'pipeline_warning',
            'reason': 'stalled',
            'dropped': self._dropped_count,
            'message': f'识别引擎已恢复，已清理积压 {purged} 句'
        })

    async def _process(self, segment: UtteranceSegment):
        """处理单条语句段：ASR → 翻译 → 广播（含时间戳透传）"""
        # ASR 未就绪（如模型切换中）时丢弃
        if not getattr(self._asr, 'is_ready', True):
            self._dropped_count += 1
            logger.debug("ASR 未就绪，语句被丢弃")
            return

        # 翻译引擎未就绪（模型切换/预载/重启中）时丢弃（与 ASR 对称）
        if not getattr(self._translator, 'is_ready', True):
            self._dropped_count += 1
            logger.debug("翻译引擎未就绪，语句被丢弃")
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
