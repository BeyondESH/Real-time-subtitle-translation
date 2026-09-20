"""
ASR 引擎模块 - 基于 sherpa-onnx Fun-ASR-Nano（INT8）的语音识别

引擎形态（replace-asr-engine-with-funasr-nano）：
- 单引擎模型（funasr-nano）：change_model 仅接受当前引擎模型标识（同值幂等，
  其他值回执 invalid_model），无档位切换路径；
- 源语言为用户设置（ja/zh/en，默认 ja）：作为识别语言提示（prompt hint），
  transcribe 返回的 language 即该值；运行时切换经 change_source_language 热生效
  ——语言在 from_funasr_nano 构造期烘焙进识别器（API 事实，1.13.8），故切换
  采用【后台原子重建识别器】：旧识别器持续服务到新识别器就绪后原子换入，
  语句零丢失、服务不中断、无需重启后端或重新下载模型；
- 解码参数经 config.yaml 的 asr 段配置（itn/hotwords/num_threads），非法值回退
  默认并告警，不中断识别可用性。

运行期健壮性（fix-asr-runtime-stall，语义保持）：
- 加载成功后执行端到端热身验证，杜绝"探针/加载通过但推理不可用"的假就绪；
- 推理走私有单线程执行器并施加与语句时长挂钩的预算，超时弃用执行器以隔离卡死线程；
- 运行期失败/超时触发一次性 CPU 降级（reason=runtime_failed），不再把模型钉死在坏设备；
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

import asr_models
from asr_models import (
    ENGINE_DISPLAY_NAME,
    ENGINE_MODEL_ID,
    ensure_asr_model_downloaded,
    is_asr_model_downloaded,
)
from device_support import (
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

# 源语言取值范围（Fun-ASR-Nano 官方支持；spec: language-handling「源语言取值范围」）
SOURCE_LANGUAGES = ('ja', 'zh', 'en')
DEFAULT_SOURCE_LANGUAGE = 'ja'

# 源语言 → 识别语言提示（from_funasr_nano 的 language 参数，构造期生效；
# 空串=无提示。取值形态按 Fun-ASR-Nano 提示词形态设定，Phase 0 实测校准点）
ASR_LANGUAGE_HINTS = {'ja': '日文', 'zh': '中文', 'en': '英文'}

# 运行期推理时间预算（秒）：max(15, 3×时长 + 10)，宽松容纳 CPU 端"慢但活着"的推理
_TRANSCRIBE_TIMEOUT_MIN_S = 15.0
_TRANSCRIBE_TIMEOUT_FACTOR = 3.0
_TRANSCRIBE_TIMEOUT_BASE_S = 10.0

# 端到端热身验证：短音频时长与预算（GPU 首次推理含上下文初始化）
WARM_UP_DURATION_S = 0.5
WARM_UP_TIMEOUT_S = 60.0

# CPU 连续失败广播阈值（每轮一次，避免刷屏）
PERSISTENT_FAILURE_THRESHOLD = 3

# ---------------------------------------------------------------------- #
# 随包 CUDA 运行库注入（spec: model-lifecycle「随包 CUDA 运行库加载（Fun-ASR-Nano）」）
# ---------------------------------------------------------------------- #

# 注入幂等标记（同目录不重复前置）
_cuda_dll_dir_added: set = set()

# 平台守卫常量（模块级便于测试注入）
_IS_WINDOWS = os.name == 'nt'


def ensure_bundled_cuda_dll_path(root: Optional[Path] = None) -> None:
    """
    将随包 CUDA 运行库目录（vendor/llama/win-x64-cuda）前置到进程 PATH。

    机制说明：onnxruntime/sherpa-onnx 以标准 LoadLibrary 语义解析 CUDA 运行库
    ——进程 PATH 注入对其同样生效（os.add_dll_directory 不覆盖 LoadLibrary
    的 PATH 搜索语义）。Windows 专用；目录缺失、异常、重复调用一律静默跳过
    （MUST NOT 影响既有加载/降级语义）。

    Args:
        root: 运行库根目录；None 时经 llama_server_manager.resolve_vendor_root()
              解析（SUBTITLE_LLAMA_DIR 优先，回落仓库 vendor/llama）
    """
    if not _IS_WINDOWS:
        return  # 非 Windows：no-op

    try:
        if root is None:
            from llama_server_manager import resolve_vendor_root

            root = resolve_vendor_root()
        cuda_dir = Path(root) / 'win-x64-cuda'
        if not cuda_dir.is_dir():
            return
        resolved = str(cuda_dir.resolve())
        if resolved in _cuda_dll_dir_added:
            return  # 幂等：同目录不重复注入
        current = os.environ.get('PATH', '')
        if resolved not in current.split(os.pathsep):
            os.environ['PATH'] = resolved + os.pathsep + current
        _cuda_dll_dir_added.add(resolved)
        logger.debug("随包 CUDA 运行库目录已加入进程 PATH: %s", resolved)
    except Exception:  # noqa: BLE001 - 注入失败不得外溢，保持既有降级语义
        logger.debug("随包 CUDA 运行库目录注入失败（忽略）", exc_info=True)


class ASREngine:
    """sherpa-onnx Fun-ASR-Nano ASR 引擎（单模型、源语言用户设置）"""

    def __init__(self, config: dict):
        self.config = config.get('asr', {})
        # 引擎模型标识：单模型语义；非法值回退引擎默认并告警
        raw_model = self.config.get('model', ENGINE_MODEL_ID)
        if raw_model != ENGINE_MODEL_ID:
            logger.warning(
                "asr.model 非法（%r），本引擎唯一模型为 %s，回退默认", raw_model, ENGINE_MODEL_ID
            )
            raw_model = ENGINE_MODEL_ID
        self.model_id = raw_model

        # self.device 语义保持为【配置偏好】（get_model_info 的 device 字段兼容依赖）
        self.device = normalize_device(self.config.get('device', 'auto'))

        # 源语言（识别提示 + transcribe 返回值；非法值回退默认并告警）
        self.source_language = self._source_language_param()

        # 解码参数（实时优先默认；非法值回退默认并告警）
        self.itn = self._bool_param('itn', True)
        self.hotwords = self._str_param('hotwords', '')
        self.num_threads = self._positive_int_param('num_threads', 2)

        # 实际使用设备与选择/降级原因（加载后填充）
        self._resolved_device: Optional[str] = None
        self._device_reason: Optional[str] = None
        self._probe: Optional[dict] = None

        # 当前识别器实际烘焙的语言提示（语言重建的判定基准）
        self._applied_language: Optional[str] = None
        self._language_rebuild_task: Optional[asyncio.Task] = None

        # 识别器缓存目录
        self.model_cache_dir = asr_models.asr_cache_root()
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)

        self._model = None  # OfflineRecognizer（进程内常驻）
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

    # ------------------------------------------------------------------ #
    # 配置参数解析（非法回退默认并告警，MUST NOT 中断识别可用性）
    # ------------------------------------------------------------------ #

    def _source_language_param(self) -> str:
        """读取源语言配置；超出取值范围回退默认 ja 并告警"""
        from language_codes import normalize_lang

        raw = self.config.get('language', DEFAULT_SOURCE_LANGUAGE)
        code = normalize_lang(raw) if isinstance(raw, str) else ''
        if code not in SOURCE_LANGUAGES:
            if 'language' in self.config:
                logger.warning(
                    "asr.language 非法（%r），支持 %s，回退默认 %s",
                    raw, list(SOURCE_LANGUAGES), DEFAULT_SOURCE_LANGUAGE,
                )
            return DEFAULT_SOURCE_LANGUAGE
        return code

    def _positive_int_param(self, key: str, default: int) -> int:
        """读取正整数配置；非法（bool/非整数/非正）回退默认值并告警"""
        raw = self.config.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
            if key in self.config:
                logger.warning("asr.%s 非法（%r），回退默认 %s", key, raw, default)
            return default
        return raw

    def _bool_param(self, key: str, default: bool) -> bool:
        """读取布尔配置；非法回退默认值并告警"""
        raw = self.config.get(key, default)
        if not isinstance(raw, bool):
            logger.warning("asr.%s 非法（%r），回退默认 %s", key, raw, default)
            return default
        return raw

    def _str_param(self, key: str, default: str) -> str:
        """读取字符串配置；非法回退默认值并告警"""
        raw = self.config.get(key, default)
        if not isinstance(raw, str):
            logger.warning("asr.%s 非法（%r），回退默认 %s", key, raw, default)
            return default
        return raw

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

    def _language_hint(self) -> str:
        """当前源语言的识别语言提示（构造期参数）"""
        return ASR_LANGUAGE_HINTS.get(self.source_language, '')

    def _detect_device(self, load_result: Optional[bool] = None,
                       probe: Optional[dict] = None) -> dict:
        """
        检测并决策 ASR 设备（经 device_support 的 onnxruntime CUDA provider 探针）

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
        - {'event':'runtime_degraded','resolved':'cpu','reason':'runtime_failed','model':str}
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
        """报告下载/加载进度"""
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
        端到端推理验证（热身）：真实走一遍解码路径。

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
        运行期降级：卸载并以 CPU 重载（reason=runtime_failed）。

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
                    "ASR 运行期不可用，自动降级 CPU（reason=runtime_failed）"
                )
                self._model = None
                self._resolved_device = None
                self._device_reason = None
                self._model = await asyncio.to_thread(
                    self._build_recognizer, 'cpu'
                )
                self._applied_language = self.source_language
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
            'model': self.model_id,
        })

    # ------------------------------------------------------------------ #
    # 源语言热切换：状态即生效 + 后台原子重建识别器（旧识别器服务到新就绪）
    # ------------------------------------------------------------------ #

    def change_source_language(self, language: str) -> str:
        """
        更新源语言（用户设置；非法值抛 ValueError，由调用方回执 invalid_language）。

        状态即时生效（transcribe 返回值、后续重建/重载的提示基准）；若引擎已
        初始化且语言确有变化，调度后台原子重建识别器（旧识别器服务到新就绪，
        语句零丢失；spec: language-handling「源语言唯一认定」）。

        Returns:
            规范化后的源语言代码

        Raises:
            ValueError: 语言超出 ja/zh/en 取值范围
        """
        from language_codes import normalize_lang

        code = normalize_lang(language) if isinstance(language, str) else ''
        if code not in SOURCE_LANGUAGES:
            raise ValueError(
                f"不支持的源语言: {language!r}，支持: {list(SOURCE_LANGUAGES)}"
            )

        previous = self.source_language
        self.source_language = code
        if code == previous:
            logger.info(f"源语言已是 {code}，无需切换")
            return code

        logger.info(f"源语言切换: {previous} -> {code}（后台重建识别提示）")
        self._schedule_language_rebuild()
        return code

    def _schedule_language_rebuild(self):
        """调度后台识别器重建（已初始化才需要；防重复调度）"""
        if not self._initialized:
            return  # 未初始化：下次 initialize 以新语言构建
        if self._language_rebuild_task is not None and not self._language_rebuild_task.done():
            return  # 已有重建在途（其锁内会取最新语言）
        try:
            self._language_rebuild_task = asyncio.create_task(
                self._rebuild_for_language()
            )
        except RuntimeError:
            logger.warning("无运行中的事件循环，源语言重建将在下次重载时应用")

    async def _rebuild_for_language(self):
        """
        后台原子重建识别器（不置 _switching：is_ready 保持 True，旧识别器持续服务）。

        锁内双检：若等待期间语言又被切换或已被重建，直接以最新状态判定。
        重建失败保留旧识别器继续服务（记录 ERROR，语言提示保持旧值生效于
        识别、新值生效于返回/翻译——两者差异在日志中可诊断）。
        """
        async with self._get_switch_lock():
            if not self._initialized or self._switching:
                return
            if self._applied_language == self.source_language:
                return  # 等待期间已被（其他重建/重载）应用
            provider = 'cuda' if self._resolved_device == 'cuda' else 'cpu'
            logger.info(
                "后台重建识别器以应用语言提示 %s（provider=%s）",
                self._language_hint(), provider,
            )
            try:
                new_model = await asyncio.to_thread(self._build_recognizer, provider)
            except Exception as e:  # noqa: BLE001 - 重建失败保留旧识别器
                logger.error(
                    "源语言识别器重建失败（保持旧提示 %s 继续服务）: %s",
                    self._applied_language, e,
                )
                return
            # 原子换入（单个属性赋值；在途调用持有旧引用不受影响）
            self._model = new_model
            self._applied_language = self.source_language
            # 新识别器必须可用：轻量热身失败同样回退旧识别器
            try:
                probe_audio = np.zeros(
                    int(SAMPLE_RATE * WARM_UP_DURATION_S), dtype=np.float32
                )
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        self._get_executor(), self._transcribe_sync, probe_audio
                    ),
                    timeout=WARM_UP_TIMEOUT_S,
                )
            except Exception as e:  # noqa: BLE001 - 新识别器不可用回退旧识别器
                logger.error(
                    "重建识别器热身失败，回退旧识别器（提示 %s）: %s",
                    self._applied_language, e,
                )
                self._applied_language = None  # 触发下次重建重试
                self._schedule_language_rebuild()
                return
            logger.info("源语言提示已应用: %s", self._language_hint())

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

        模型文件缺失时先下载（进度经 model_progress 可见）再加载。偏好 × 探针
        决策设备：探针不可用直接 CPU；尝试 GPU 加载失败静默重试 CPU
        （reason=load_failed）；加载成功但端到端验证失败同样降级
        （reason=runtime_failed）。CPU 加载或验证仍失败才冒泡。
        """
        if self._initialized:
            return
        async with self._get_switch_lock():
            if self._initialized:
                return
            await self._initialize_locked()

    async def _initialize_locked(self):
        """锁内初始化（启动加载与各重载路径共用）"""
        logger.info(f"正在加载识别模型: {ENGINE_DISPLAY_NAME}（{asr_models.ASR_MODEL_ID}）")
        self._report_progress(ENGINE_DISPLAY_NAME, 0, "准备加载模型...")
        start_time = time.time()

        # 新一轮加载：重置运行期降级/失败计数状态
        self._runtime_degraded = False
        self._consecutive_failures = 0
        self._persistent_failure_notified = False

        await self._ensure_model_files()
        await self._load_with_fallback()

        elapsed = time.time() - start_time
        self._report_progress(ENGINE_DISPLAY_NAME, 100, f"模型加载完成 ({elapsed:.1f}s)")
        logger.info(
            "模型加载完成，耗时: %.2fs，偏好: %s，实际设备: %s（reason=%s）",
            elapsed, self.device, self._resolved_device, self._device_reason,
        )
        self._initialized = True

    async def _ensure_model_files(self):
        """模型文件缺失时先下载（进度可见）；失败按初始化失败语义冒泡"""
        if is_asr_model_downloaded():
            return
        self._report_progress(ENGINE_DISPLAY_NAME, 0, "识别模型缺失，开始下载…")

        def on_progress(name: str, pct: float, msg: str):
            self._report_progress(name, pct, msg)

        await ensure_asr_model_downloaded(progress=on_progress)

    async def _load_with_fallback(self):
        """
        统一加载路径：探针决策 → 尝试 GPU（加载 + 端到端验证）→ 失败静默降级 CPU。

        GPU 加载失败 reason=load_failed；加载成功但端到端验证失败 reason=runtime_failed。
        CPU 路径同样执行端到端验证，验证失败按初始化失败语义冒泡。
        """
        # 随包 CUDA 运行库注入：须先于 GPU 构造识别器（幂等；非 Windows/缺目录 no-op）
        ensure_bundled_cuda_dll_path()

        probe = probe_compute()['asr']
        decision = self._detect_device(probe=probe)

        if decision['attempt_cuda']:
            try:
                self._model = await asyncio.to_thread(self._build_recognizer, 'cuda')
            except Exception as e:  # noqa: BLE001 - GPU 加载失败静默降级，不冒泡
                logger.warning(
                    "ASR GPU 加载失败，静默降级 CPU（偏好=%s，reason=load_failed，探针=%s）：%s",
                    self.device, probe.get('detail'), e,
                )
                self._model = None
                decision = self._detect_device(load_result=False, probe=probe)
            else:
                self._applied_language = self.source_language
                try:
                    await self._warm_up_validate()
                except Exception as e:  # noqa: BLE001 - 运行期不可用降级
                    logger.warning(
                        "ASR GPU 端到端验证失败（加载成功但推理不可用），"
                        "降级 CPU（reason=runtime_failed）：%s", e,
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
        self._model = await asyncio.to_thread(self._build_recognizer, 'cpu')
        self._applied_language = self.source_language
        self._resolved_device = 'cpu'
        self._device_reason = decision['reason']
        await self._warm_up_validate()  # CPU 验证失败 → 冒泡（初始化失败语义）

        if self._device_reason == 'runtime_failed':
            logger.warning(
                "ASR 已回退 CPU（reason=runtime_failed）：目标设备加载成功但运行时不可用"
            )
        elif decision['reason'] == 'no_cuda' and self.device != 'cpu':
            logger.warning(
                "ASR 未检测到可用 CUDA，回退 CPU（偏好=%s，探针: %s）",
                self.device, probe.get('detail'),
            )
        else:
            logger.info("ASR 使用 CPU（reason=%s）", decision['reason'])

    def _build_recognizer(self, provider: str):
        """
        构造 OfflineRecognizer（同步，在线程中运行；模型文件须已就绪）

        Args:
            provider: 'cuda' | 'cpu'

        Returns:
            sherpa_onnx.OfflineRecognizer
        """
        import sherpa_onnx

        paths = asr_models.asr_model_paths()
        hint = self._language_hint()
        logger.info(
            "构造 Fun-ASR-Nano 识别器（provider=%s, threads=%d, language=%r, itn=%s）",
            provider, self.num_threads, hint, self.itn,
        )
        try:
            return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
                encoder_adaptor=str(paths['encoder_adaptor']),
                llm=str(paths['llm']),
                embedding=str(paths['embedding']),
                tokenizer=str(paths['tokenizer']),
                provider=provider,
                num_threads=self.num_threads,
                language=hint,
                itn=self.itn,
                hotwords=self.hotwords,
            )
        except Exception as e:
            logger.error("构造识别模型失败: %s", e)
            raise

    async def transcribe(self, audio_data: np.ndarray) -> Optional[dict]:
        """
        识别音频数据

        Args:
            audio_data: 音频数据 (float32, 16kHz, mono)

        Returns:
            识别结果，包含 text / language（=源语言设置）/ confidence（恒为 None，
            新引擎无该概念）；失败/超时返回 None（不抛异常）
        """
        if not self._initialized or self._model is None:
            logger.error("ASR 引擎未初始化")
            return None

        if len(audio_data) == 0:
            return None

        try:
            start_time = time.time()

            # 私有执行器 + 时长挂钩预算（超时弃用执行器，隔离卡死线程）
            text = await self._run_transcribe_bounded(
                audio_data, self._transcribe_timeout_s(audio_data)
            )

            elapsed = time.time() - start_time
            logger.debug(
                f"识别完成，耗时: {elapsed:.3f}s，源语言: {self.source_language}"
            )

            # 成功即清零连续失败计数
            self._consecutive_failures = 0
            self._persistent_failure_notified = False

            if not text or not text.strip():
                return None

            return {
                'text': text.strip(),
                'language': self.source_language,
                'confidence': None,
            }

        except asyncio.TimeoutError:
            self._record_runtime_failure("推理超时")
            return None
        except Exception as e:
            logger.error(f"识别错误: {e}")
            self._record_runtime_failure(str(e))
            return None

    def _transcribe_sync(self, audio_data: np.ndarray) -> str:
        """
        同步识别（在线程中运行）

        Args:
            audio_data: 音频数据

        Returns:
            识别文本（可能为空串）
        """
        # 确保音频数据格式正确
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)

        # 如果是单声道但形状不对，调整一下
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()

        stream = self._model.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio_data)
        self._model.decode_stream(stream)
        return stream.result.text

    async def change_model(self, model: str):
        """
        切换模型（单引擎模型语义：同值幂等，其他值回执 invalid_model）

        Args:
            model: 引擎模型标识（仅接受当前引擎唯一模型）

        Raises:
            ValueError: 模型标识不受支持
        """
        if model != self.model_id:
            raise ValueError(
                f"不支持的模型: {model}，本引擎唯一模型: {self.model_id}"
                "（Whisper 档位已移除）"
            )

        if self._initialized:
            logger.info(f"模型 {model} 已加载")
            return

        async with self._get_switch_lock():
            # 锁内双检：并发的相同切换请求只生效一次
            if self._initialized:
                logger.info(f"模型 {model} 已加载")
                return

            self._switching = True
            try:
                logger.info(f"按请求加载引擎模型: {model}")
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
                self._model = None  # 释放旧识别器
                self._resolved_device = None
                self._device_reason = None
                self._applied_language = None

                await self._initialize_locked()
            finally:
                self._switching = False

    def get_model_info(self) -> dict:
        """
        获取当前模型信息（get_config 的 asr 段；无 Whisper 档位字段）

        Returns:
            模型信息字典（device=配置偏好；resolved_device/device_reason=实际结果；
            language=当前源语言）
        """
        return {
            'model': self.model_id,
            'display_name': ENGINE_DISPLAY_NAME,
            'language': self.source_language,
            'device': self.device,
            'resolved_device': self._resolved_device,
            'device_reason': self._device_reason,
            'initialized': self._initialized,
        }
