"""
ASR 引擎模块 - 基于 faster-whisper 的语音识别

运行期健壮性（fix-asr-runtime-stall）：
- 加载成功后执行端到端热身验证，杜绝"探针/加载通过但推理不可用"的假就绪
  （实证：有驱动无 cuBLAS 时加载成功、首次推理报错、后续调用挂起）；
- 推理走私有单线程执行器并施加与语句时长挂钩的预算，超时弃用执行器以隔离卡死线程；
- 运行期失败/超时触发一次性 CPU+int8 降级（reason=runtime_failed），不再把模型钉死在坏设备；
- 启动初始化与一切重载路径共用同一把串行化锁，消除并发双加载/后写覆盖竞态。
"""
import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from faster_whisper import WhisperModel

from device_support import (
    CPU_COMPUTE_TYPE,
    VALID_DEVICES,
    decide_device,
    normalize_device,
    probe_compute,
)

logger = logging.getLogger(__name__)

# 模型下载进度回调类型
DownloadProgressCallback = Callable[[str, float, str], None]

# ASR 健康事件回调类型（运行期降级 / CPU 连续失败），事件循环上下文触发
HealthCallback = Callable[[dict], None]

# 推理音频采样率（管线统一契约）
SAMPLE_RATE = 16000

# 运行期推理时间预算（秒）：max(15, 3×时长 + 10)，宽松容纳 CPU 端"慢但活着"的推理
_TRANSCRIBE_TIMEOUT_MIN_S = 15.0
_TRANSCRIBE_TIMEOUT_FACTOR = 3.0
_TRANSCRIBE_TIMEOUT_BASE_S = 10.0

# 端到端热身验证：短音频时长与预算（GPU 首次推理含上下文初始化）
WARM_UP_DURATION_S = 0.5
WARM_UP_TIMEOUT_S = 60.0

# CPU 连续失败广播阈值（每轮一次，避免刷屏）
PERSISTENT_FAILURE_THRESHOLD = 3


class ASREngine:
    """faster-whisper ASR 引擎"""

    # 支持的模型大小
    SUPPORTED_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3']

    # 模型大小映射 (模型名 -> 大小描述)
    MODEL_SIZES = {
        'tiny': '39MB',
        'base': '74MB',
        'small': '244MB',
        'medium': '769MB',
        'large-v3': '1.5GB'
    }

    def __init__(self, config: dict):
        self.config = config.get('asr', {})
        self.model_size = self.config.get('model_size', 'base')
        # self.device 语义保持为【配置偏好】（get_model_info 的 device 字段兼容依赖）
        self.device = normalize_device(self.config.get('device', 'auto'))
        self.language = self.config.get('language', None)
        self.compute_type = self.config.get('compute_type', 'float16')

        # 实际使用设备与选择/降级原因（加载后填充）
        self._resolved_device: Optional[str] = None
        self._device_reason: Optional[str] = None
        self._probe: Optional[dict] = None

        # 模型缓存目录
        self.model_cache_dir = Path.home() / '.cache' / 'subtitle-translator' / 'whisper'
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)

        self._model: Optional[WhisperModel] = None
        self._initialized = False
        self._switching = False
        self._switch_lock: Optional[asyncio.Lock] = None  # 延迟创建（需事件循环）
        self._download_progress_callback: Optional[DownloadProgressCallback] = None

        # 运行期健壮性状态
        self._executor: Optional[ThreadPoolExecutor] = None  # 私有单线程执行器（可弃用重建）
        self._runtime_degraded = False  # 已因运行期不可用自动降级（一次性，防重复触发）
        self._consecutive_failures = 0  # 连续推理失败计数（成功即清零）
        self._persistent_failure_notified = False  # 本轮连续失败是否已广播
        self._health_callback: Optional[HealthCallback] = None

    @property
    def is_ready(self) -> bool:
        """模型已初始化且不在切换中"""
        return self._initialized and not self._switching

    @property
    def resolved_device(self) -> Optional[str]:
        """实际使用设备（'cuda' | 'cpu' | None=尚未加载）"""
        return self._resolved_device

    @property
    def device_reason(self) -> Optional[str]:
        """设备选择/降级原因（auto | user | no_cuda | load_failed | runtime_failed | None）"""
        return self._device_reason

    def _detect_device(self, load_result: Optional[bool] = None,
                       probe: Optional[dict] = None) -> dict:
        """
        检测并决策 ASR 设备（经 device_support 的 CTranslate2 探针）

        Args:
            load_result: None=尚未尝试；False=GPU 加载失败（触发 load_failed）
            probe: 复用外部已取得的探针结果，避免重复探测

        Returns:
            device_support.decide_device 的决策字典
        """
        if probe is None:
            probe = probe_compute()['asr']
        self._probe = probe
        return decide_device(self.device, probe, load_result=load_result)

    def set_download_progress_callback(self, callback: DownloadProgressCallback):
        """
        设置下载进度回调函数

        Args:
            callback: 回调函数，参数为 (model_name, progress_percent, status_message)
        """
        self._download_progress_callback = callback

    def set_health_callback(self, callback: Optional[HealthCallback]):
        """
        设置健康事件回调（运行期降级 / CPU 连续失败）。

        事件形状：
        - {'event':'runtime_degraded','resolved':'cpu','reason':'runtime_failed','model_size':str}
        - {'event':'persistent_failure','failures':int,'last_error':str}
        回调在事件循环上下文触发；回调异常 MUST NOT 外溢。
        """
        self._health_callback = callback

    def _emit_health(self, payload: dict):
        """触发健康回调（异常只记日志，不影响引擎运行）"""
        callback = self._health_callback
        if callback is None:
            return
        try:
            callback(payload)
        except Exception:  # noqa: BLE001 - 回调异常不得影响引擎
            logger.exception("ASR 健康回调异常")

    def _report_progress(self, model_name: str, progress: float, message: str):
        """报告下载进度"""
        if self._download_progress_callback:
            self._download_progress_callback(model_name, progress, message)
        logger.info(f"[{model_name}] {progress:.1f}% - {message}")

    # ------------------------------------------------------------------ #
    # 推理执行：私有执行器 + 时长挂钩预算（超时弃用执行器，隔离卡死线程）
    # ------------------------------------------------------------------ #

    def _get_executor(self) -> ThreadPoolExecutor:
        """获取私有单线程执行器（懒创建）"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix='asr-transcribe'
            )
        return self._executor

    def _reset_executor(self):
        """弃用当前执行器：卡死线程被隔离，后续调用使用新执行器"""
        executor = self._executor
        self._executor = None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 - 弃用失败不阻断
                logger.debug("执行器弃用异常（忽略）", exc_info=True)

    def _transcribe_timeout_s(self, audio_data: np.ndarray) -> float:
        """推理时间预算：max(15, 3×时长 + 10) 秒"""
        duration = len(audio_data) / SAMPLE_RATE
        return max(
            _TRANSCRIBE_TIMEOUT_MIN_S,
            _TRANSCRIBE_TIMEOUT_FACTOR * duration + _TRANSCRIBE_TIMEOUT_BASE_S,
        )

    async def _run_transcribe_bounded(self, audio_data: np.ndarray, timeout_s: float):
        """在私有执行器中执行同步推理；超时则弃用执行器并抛 asyncio.TimeoutError。"""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            self._get_executor(), self._transcribe_sync, audio_data
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "ASR 推理超时（预算 %.1fs），弃用当前执行器以隔离卡死调用", timeout_s
            )
            self._reset_executor()
            raise

    async def _warm_up_validate(self):
        """
        端到端推理验证（热身）：真实走一遍推理路径。

        "加载成功 ≠ 可推理"——缺运行库时首次调用可能报错、后续调用可能挂起。
        失败抛 RuntimeError（由加载路径决定降级/冒泡）。
        """
        probe_audio = np.zeros(int(SAMPLE_RATE * WARM_UP_DURATION_S), dtype=np.float32)
        try:
            await self._run_transcribe_bounded(probe_audio, WARM_UP_TIMEOUT_S)
        except asyncio.TimeoutError as e:
            raise RuntimeError("端到端验证超时（推理调用未返回）") from e
        except Exception as e:  # noqa: BLE001 - 统一转为验证失败
            raise RuntimeError(f"端到端验证失败: {e}") from e

    # ------------------------------------------------------------------ #
    # 运行期失败记录与一次性降级
    # ------------------------------------------------------------------ #

    def _record_runtime_failure(self, detail: str):
        """记录一次运行期推理失败：cuda → 触发一次性降级；cpu → 连续失败计数。"""
        self._consecutive_failures += 1

        if self._resolved_device == 'cuda' and not self._runtime_degraded:
            logger.warning("ASR 运行期推理失败（cuda）：%s → 触发一次性降级", detail)
            self._schedule_degrade()
            return

        if (
            self._resolved_device == 'cpu'
            and self._consecutive_failures >= PERSISTENT_FAILURE_THRESHOLD
            and not self._persistent_failure_notified
        ):
            self._persistent_failure_notified = True
            logger.error(
                "ASR CPU 连续失败 %d 次：%s", self._consecutive_failures, detail
            )
            self._emit_health({
                'event': 'persistent_failure',
                'failures': self._consecutive_failures,
                'last_error': detail,
            })

    def _schedule_degrade(self):
        """调度一次性运行期降级（须在事件循环上下文；无循环时仅记日志）。"""
        if self._runtime_degraded or self._switching:
            return
        try:
            asyncio.create_task(self._degrade_runtime_to_cpu())
        except RuntimeError:
            logger.warning("无运行中的事件循环，无法调度 ASR 运行期降级")

    def notify_stall(self):
        """管线停滞监管触发的健康处置入口：仍在 cuda 时触发一次性降级。"""
        if self._resolved_device != 'cuda' or self._runtime_degraded or self._switching:
            return
        logger.warning("管线停滞监管触发 ASR 健康处置（当前设备 cuda）")
        self._schedule_degrade()

    async def _degrade_runtime_to_cpu(self):
        """
        运行期降级：卸载并以 CPU+int8 重载（reason=runtime_failed）。

        一次性 + 防抖；失败时标记未初始化（后续由显式重载恢复），不冒泡。
        完成后触发健康回调（事件循环上下文）。
        """
        if self._runtime_degraded:
            return
        async with self._get_switch_lock():
            if self._runtime_degraded or self._switching:
                return
            self._switching = True
            try:
                logger.warning(
                    "ASR 运行期不可用，自动降级 CPU+int8（reason=runtime_failed）"
                )
                self._model = None
                self._resolved_device = None
                self._device_reason = None
                self._model = await asyncio.to_thread(
                    self._load_model_with_progress,
                    self.model_size,
                    'cpu',
                    CPU_COMPUTE_TYPE,
                )
                await self._warm_up_validate()
                self._resolved_device = 'cpu'
                self._device_reason = 'runtime_failed'
                self._runtime_degraded = True
                self._consecutive_failures = 0
                self._persistent_failure_notified = False
                logger.info("ASR 运行期降级完成：cpu/runtime_failed")
            except Exception as e:  # noqa: BLE001 - 降级失败不冒泡
                logger.error("ASR 运行期降级失败: %s", e)
                self._model = None
                self._initialized = False
                return
            finally:
                self._switching = False

        self._emit_health({
            'event': 'runtime_degraded',
            'resolved': 'cpu',
            'reason': 'runtime_failed',
            'model_size': self.model_size,
        })

    # ------------------------------------------------------------------ #
    # 加载与初始化（同一把串行化锁覆盖一切重载路径）
    # ------------------------------------------------------------------ #

    def _get_switch_lock(self) -> asyncio.Lock:
        """获取串行化锁（懒创建，须在事件循环线程调用）"""
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        return self._switch_lock

    async def initialize(self):
        """
        初始化 ASR 引擎（锁串行化 + 双检；含加载/运行期双阶段静默降级）

        偏好 × 探针决策设备：探针不可用直接 CPU+int8；尝试 GPU 加载失败
        静默重试 CPU+int8（reason=load_failed）；加载成功但端到端验证失败
        同样降级（reason=runtime_failed）。CPU 加载或验证仍失败才冒泡。
        """
        if self._initialized:
            return
        async with self._get_switch_lock():
            if self._initialized:
                return
            await self._initialize_locked()

    async def _initialize_locked(self):
        """锁内初始化（启动加载与各重载路径共用）"""
        logger.info(f"正在加载 Whisper 模型: {self.model_size}")
        self._report_progress(self.model_size, 0, "准备加载模型...")
        start_time = time.time()

        # 新一轮加载：重置运行期降级/失败计数状态
        self._runtime_degraded = False
        self._consecutive_failures = 0
        self._persistent_failure_notified = False

        await self._load_with_fallback()

        elapsed = time.time() - start_time
        self._report_progress(self.model_size, 100, f"模型加载完成 ({elapsed:.1f}s)")
        logger.info(
            "模型加载完成，耗时: %.2fs，偏好: %s，实际设备: %s（reason=%s）",
            elapsed, self.device, self._resolved_device, self._device_reason,
        )
        self._initialized = True

    async def _load_with_fallback(self):
        """
        统一加载路径：探针决策 → 尝试 GPU（加载 + 端到端验证）→ 失败静默降级 CPU+int8。

        GPU 加载失败 reason=load_failed；加载成功但端到端验证失败 reason=runtime_failed。
        CPU 路径同样执行端到端验证，验证失败按初始化失败语义冒泡。
        """
        probe = probe_compute()['asr']
        decision = self._detect_device(probe=probe)

        self._report_progress(
            self.model_size, 10,
            f"正在下载模型 ({self.MODEL_SIZES.get(self.model_size, '未知')})...",
        )

        if decision['attempt_cuda']:
            try:
                self._model = await asyncio.to_thread(
                    self._load_model_with_progress,
                    self.model_size,
                    'cuda',
                    self.compute_type,
                )
            except Exception as e:  # noqa: BLE001 - GPU 加载失败静默降级，不冒泡
                logger.warning(
                    "ASR GPU 加载失败，静默降级 CPU+int8（偏好=%s，reason=load_failed，探针=%s）：%s",
                    self.device, probe.get('detail'), e,
                )
                self._model = None
                decision = self._detect_device(load_result=False, probe=probe)
            else:
                try:
                    await self._warm_up_validate()
                except Exception as e:  # noqa: BLE001 - 运行期不可用降级
                    logger.warning(
                        "ASR GPU 端到端验证失败（加载成功但推理不可用），"
                        "降级 CPU+int8（reason=runtime_failed）：%s", e,
                    )
                    self._model = None
                    decision = {
                        'resolved': 'cpu',
                        'reason': 'runtime_failed',
                        'attempt_cuda': False,
                    }
                else:
                    self._resolved_device = 'cuda'
                    self._device_reason = decision['reason']
                    logger.info(
                        "ASR 使用 CUDA（reason=%s）；探针: %s",
                        decision['reason'], probe.get('detail'),
                    )
                    return

        # CPU 路径：探针不可用（no_cuda）/ GPU 加载失败（load_failed）/ 运行期不可用（runtime_failed）/ 显式 cpu
        self._model = await asyncio.to_thread(
            self._load_model_with_progress,
            self.model_size,
            'cpu',
            CPU_COMPUTE_TYPE,
        )
        self._resolved_device = 'cpu'
        self._device_reason = decision['reason']
        await self._warm_up_validate()  # CPU 验证失败 → 冒泡（初始化失败语义）

        if self._device_reason == 'runtime_failed':
            logger.warning(
                "ASR 已回退 CPU+int8（reason=runtime_failed）：目标设备加载成功但运行时不可用"
            )
        elif decision['reason'] == 'no_cuda' and self.device != 'cpu':
            logger.warning(
                "ASR 未检测到可用 CUDA，回退 CPU+int8（偏好=%s，探针: %s）",
                self.device, probe.get('detail'),
            )
        else:
            logger.info("ASR 使用 CPU+int8（reason=%s）", decision['reason'])

    def _load_model_with_progress(self, model_size: str, device: str, compute_type: str):
        """
        加载模型（同步，在线程中运行）

        Args:
            model_size: 模型大小
            device: 计算设备
            compute_type: 计算类型

        Returns:
            加载的模型
        """
        try:
            # faster-whisper 会自动处理模型下载
            # 模型会缓存在 ~/.cache/huggingface/ 或指定目录
            model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
                download_root=str(self.model_cache_dir)
            )
            return model

        except Exception as e:
            logger.error(f"加载模型失败: {e}")
            raise

    async def transcribe(self, audio_data: np.ndarray) -> Optional[dict]:
        """
        识别音频数据

        Args:
            audio_data: 音频数据 (float32, 16kHz, mono)

        Returns:
            识别结果，包含 text 和 language 字段；失败/超时返回 None（不抛异常）
        """
        if not self._initialized or self._model is None:
            logger.error("ASR 引擎未初始化")
            return None

        if len(audio_data) == 0:
            return None

        try:
            start_time = time.time()

            # 私有执行器 + 时长挂钩预算（超时弃用执行器，隔离卡死线程）
            segments, info = await self._run_transcribe_bounded(
                audio_data, self._transcribe_timeout_s(audio_data)
            )

            # 合并所有片段
            text = ''.join([segment.text for segment in segments]).strip()

            elapsed = time.time() - start_time
            logger.debug(f"识别完成，耗时: {elapsed:.3f}s，语言: {info.language}")

            # 成功即清零连续失败计数
            self._consecutive_failures = 0
            self._persistent_failure_notified = False

            if not text:
                return None

            return {
                'text': text,
                'language': info.language,
                'confidence': info.language_probability
            }

        except asyncio.TimeoutError:
            self._record_runtime_failure("推理超时")
            return None
        except Exception as e:
            logger.error(f"识别错误: {e}")
            self._record_runtime_failure(str(e))
            return None

    def _transcribe_sync(self, audio_data: np.ndarray):
        """
        同步识别（在线程中运行）

        Args:
            audio_data: 音频数据

        Returns:
            (segments, info) 元组
        """
        # 确保音频数据格式正确
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)

        # 如果是单声道但形状不对，调整一下
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()

        segments, info = self._model.transcribe(
            audio_data,
            language=self.language,
            beam_size=5,
            vad_filter=True,  # 启用 VAD 过滤静音
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200
            )
        )

        return list(segments), info

    async def change_model(self, model_size: str):
        """
        切换 Whisper 模型（锁串行化，切换期间 is_ready=False，语句被丢弃）

        Args:
            model_size: 新的模型大小

        Raises:
            ValueError: 模型名不受支持
        """
        if model_size not in self.SUPPORTED_MODELS:
            raise ValueError(f"不支持的模型: {model_size}，支持: {self.SUPPORTED_MODELS}")

        if model_size == self.model_size and self._initialized:
            logger.info(f"模型 {model_size} 已加载")
            return

        async with self._get_switch_lock():
            # 锁内双检：并发的相同切换请求只生效一次
            if model_size == self.model_size and self._initialized:
                logger.info(f"模型 {model_size} 已加载")
                return

            self._switching = True
            try:
                logger.info(f"切换模型: {self.model_size} -> {model_size}")
                self.model_size = model_size
                self._initialized = False
                self._model = None  # 释放旧模型

                await self._initialize_locked()
            finally:
                self._switching = False

    async def change_device(self, device: str):
        """
        切换推理设备（与 change_model 共用 _switch_lock，同值幂等）

        Args:
            device: 'auto' | 'cpu' | 'cuda'

        Raises:
            ValueError: 设备值不受支持
        """
        if device not in VALID_DEVICES:
            raise ValueError(f"不支持的设备: {device}，支持: {VALID_DEVICES}")

        if device == self.device and self._initialized:
            logger.info(f"ASR 设备偏好已是 {device}（实际 {self._resolved_device}）")
            return

        async with self._get_switch_lock():
            # 锁内双检：并发的相同切换请求只生效一次
            if device == self.device and self._initialized:
                logger.info(f"ASR 设备偏好已是 {device}（实际 {self._resolved_device}）")
                return

            self._switching = True
            try:
                logger.info(f"切换 ASR 设备: {self.device} -> {device}")
                self.device = device
                self._initialized = False
                self._model = None  # 释放旧模型
                self._resolved_device = None
                self._device_reason = None

                await self._initialize_locked()
            finally:
                self._switching = False

    def get_model_info(self) -> dict:
        """
        获取当前模型信息

        Returns:
            模型信息字典（device=配置偏好；resolved_device/device_reason=实际结果）
        """
        return {
            'model_size': self.model_size,
            'device': self.device,
            'resolved_device': self._resolved_device,
            'device_reason': self._device_reason,
            'compute_type': self.compute_type,
            'initialized': self._initialized,
            'supported_models': self.SUPPORTED_MODELS
        }
