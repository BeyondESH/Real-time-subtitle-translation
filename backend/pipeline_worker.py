"""
管线工作器：有界语句队列 + ASR + 翻译 + WebSocket 广播

背压策略：队列满时丢弃最旧语句并向客户端广播 pipeline_warning（reason=queue_full），
绝不为语句创建无界并发任务。

停滞监管（fix-asr-runtime-stall）：进度心跳 + 独立监管任务；超过 stall_timeout_s
无处理进度时广播 stalled、清空过期积压（计入丢弃数）、触发引擎健康处置并继续消费，
恢复 MUST NOT 要求重启后端。单条语句受时间预算约束，超时丢弃并继续（活性兜底）。
"""
import asyncio
import inspect
import logging
import time
from typing import Awaitable, Callable, Optional, Union

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


def _supports_partial_callback(translator: object) -> bool:
    """
    判断翻译器是否显式声明 on_partial 形参（流式回调能力自描述）。

    真机 Translator.translate_with_metrics 声明了该形参；不支持流式回调的旧
    实现/测试双打器（如裸 AsyncMock，签名为 (*args, **kwargs)）跳过传参，
    保持其既有调用契约不变。
    """
    method = getattr(translator, 'translate_with_metrics', None)
    if method is None:
        return False
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return 'on_partial' in params


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
        # 进程内单调语句标识计数（partial/final/cancel 贯穿；不复用）
        self._id_seq = 0

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

        # 进程内单调语句标识：随 partial/final/cancel 贯穿（兼容路径同样分配）
        self._id_seq += 1
        segment.id = f"u{self._id_seq}"

        # 入队时刻（延迟埋点：queue_ms 的起点；兼容路径同样记录）
        segment.enqueued_at = time.monotonic()

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
        """
        处理单条语句段：ASR → 流式翻译（增量广播 partial）→ 定稿广播。

        partial 生命周期：已广播进行中帧而未成功定稿的退出路径（超时取消/异常）
        由 finally 以 fire-and-forget 方式补发 subtitle_cancel 清算；未推过
        partial 的路径（引擎未就绪/空识别结果）MUST NOT 产生清算帧。
        """
        # ASR 未就绪（如模型切换中）时丢弃（未推 partial，无需清算）
        if not getattr(self._asr, 'is_ready', True):
            self._dropped_count += 1
            logger.debug("ASR 未就绪，语句被丢弃")
            return

        # 翻译引擎未就绪（模型切换/预载/重启中）时丢弃（与 ASR 对称）
        if not getattr(self._translator, 'is_ready', True):
            self._dropped_count += 1
            logger.debug("翻译引擎未就绪，语句被跳过")
            return

        segment_id: Optional[str] = getattr(segment, 'id', None)
        partial_sent = False
        first_partial_at: Optional[float] = None
        cancel_reason = 'dropped'
        finished = False
        try:
            started = time.monotonic()
            queue_ms = (
                (started - segment.enqueued_at) * 1000.0
                if segment.enqueued_at is not None else None
            )

            asr_started = time.monotonic()
            transcription = await self._asr.transcribe(segment.audio)
            asr_ms = (time.monotonic() - asr_started) * 1000.0
            if not transcription or not transcription.get('text'):
                return

            # 只翻译激活目标语言（多语言全翻会放大推理延迟）
            active = self._get_active_language() if self._get_active_language else None
            targets = [active] if active else None

            async def on_partial(
                target_lang: str, accumulated: str, is_first: bool
            ) -> None:
                """广播进行中帧：同一句共享 id，translations 为该语言累积译文"""
                nonlocal partial_sent, first_partial_at
                await self._ws.send({
                    'type': 'subtitle_partial',
                    'id': segment_id,
                    'original': transcription['text'],
                    'source_language': transcription['language'],
                    'active_language': active,
                    'translations': {target_lang: accumulated},
                })
                partial_sent = True
                # 首帧时刻以广播完成为准（first_token_ms 起点为切句时刻）
                if is_first and first_partial_at is None:
                    first_partial_at = time.monotonic()

            llm_started = time.monotonic()
            translate_kwargs: dict = {}
            if _supports_partial_callback(self._translator):
                translate_kwargs['on_partial'] = on_partial
            translations, tps_by_lang = await self._translator.translate_with_metrics(
                transcription['text'],
                transcription['language'],
                targets,
                **translate_kwargs,
            )
            llm_ms = (time.monotonic() - llm_started) * 1000.0

            payload = {
                'type': 'subtitle',
                'id': segment_id,
                'original': transcription['text'],
                'source_language': transcription['language'],
                'active_language': active,
                'translations': translations
            }
            # 首字时延（切句 → 首个进行中帧）：仅流式且实际产生首帧时携带
            if first_partial_at is not None and segment.cut_at is not None:
                payload['first_token_ms'] = int(round(max(
                    0.0, (first_partial_at - segment.cut_at) * 1000.0
                )))
            # LLM 生成速度透传：仅激活语言实际生成成功时携带；缺失不附加字段
            if active and active in tps_by_lang:
                payload['tps'] = tps_by_lang[active]
            # 时间戳透传（SRT 精确导出的前提）；兼容路径缺失时不附加字段
            if segment.ts_start is not None and segment.ts_end is not None:
                payload['ts_start'] = round(segment.ts_start, 3)
                payload['ts_end'] = round(segment.ts_end, 3)
            # 分阶段耗时透传（全有或全无）：兼容路径（无切句/入队时刻）不携带
            latency = self._build_latency(segment, queue_ms, asr_ms, llm_ms)
            if latency is not None:
                payload['latency'] = latency
                logger.info(
                    "端到端 %.2fs（静音等待 %.2fs / 队列 %.2fs / 识别 %.2fs / 翻译 %.2fs）",
                    (latency['endpoint_ms'] + latency['total_ms']) / 1000.0,
                    latency['endpoint_ms'] / 1000.0,
                    latency['queue_ms'] / 1000.0,
                    latency['asr_ms'] / 1000.0,
                    latency['llm_ms'] / 1000.0,
                )

            await self._ws.send(payload)
            finished = True
        except asyncio.CancelledError:
            # segment_budget_s 超时取消（wait_for 取消）：清算原因 timeout
            cancel_reason = 'timeout'
            raise
        except Exception:
            # 请求/处理异常：_run 保留既有日志与超时处理，这里仅记录清算原因
            cancel_reason = 'failed'
            raise
        finally:
            # 每个已广播的进行中帧必有终结；取消清理路径不得 await
            if partial_sent and not finished:
                asyncio.create_task(self._ws.send({
                    'type': 'subtitle_cancel',
                    'id': segment_id,
                    'reason': cancel_reason,
                }))

    def _build_latency(
        self, segment: UtteranceSegment,
        queue_ms: Optional[float], asr_ms: float, llm_ms: float,
    ) -> Optional[dict]:
        """组装分阶段耗时（5 键齐全、非负整数 ms）；任一来源缺失返回 None（全有或全无）"""
        if (
            segment.cut_at is None
            or segment.silence_wait_s is None
            or queue_ms is None
        ):
            return None
        return {
            'endpoint_ms': int(round(max(0.0, segment.silence_wait_s * 1000.0))),
            'queue_ms': int(round(max(0.0, queue_ms))),
            'asr_ms': int(round(max(0.0, asr_ms))),
            'llm_ms': int(round(max(0.0, llm_ms))),
            'total_ms': int(round(max(0.0, (time.monotonic() - segment.cut_at) * 1000.0))),
        }
