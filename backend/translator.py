"""
翻译引擎模块 —— llama.cpp（llama-server sidecar）本地翻译

架构（design.md D1/D3/D4/D5/D7）：
- 推理在随包的 llama-server 子进程中执行，经回环 HTTP `/v1/chat/completions` 调用
  （进程级隔离：推理崩溃不拖垮后端，kill 即彻底释放显存）；
- 模型来自随包静态注册表（translation_models），文件按需下载（model_downloader）；
- prompt 模板与采样参数按模型族内置（translation_profiles），目标语言名经
  language_codes 显式映射注入；
- 设备：llama-server 子进程按设备选择双构建（cuda → CUDA 版 -ngl 99；cpu → CPU 版 -ngl 0），
  探针与静默降级语义经 device_support（no_cuda / load_failed / runtime_failed）。

对外接口保持与旧实现兼容：translate / change_device / get_model_info；
新增 is_ready / change_llm / translate_with_metrics / ensure_default /
set_health_callback / stop。
"""
import asyncio
import json
import logging
import re
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import httpx

from device_support import (
    VALID_DEVICES,
    decide_device,
    normalize_device,
    probe_compute,
)
from language_codes import (
    normalize_lang,
    to_prompt_language_name,
    unsupported_pair_message,
)
from llama_server_manager import (
    LlamaServerManager,
    ServerConfig,
    resolve_device_binary,
)
from model_downloader import download_model
from translation_models import (
    DEFAULT_MODEL_ID,
    REGISTRY,
    TranslationModel,
    default_model,
    get_model,
    is_downloaded,
    model_path,
)
from translation_profiles import build_messages, get_profile

logger = logging.getLogger(__name__)

# 模型下载进度回调类型（形状与 ASR 侧一致：name/percent/message）
DownloadProgressCallback = Callable[[str, float, str], None]

# 健康事件回调类型（运行期降级 / 连续失败），事件循环上下文触发
HealthCallback = Callable[[dict], None]

# CPU 连续失败广播阈值（每轮一次，避免刷屏）
PERSISTENT_FAILURE_THRESHOLD = 3

# 退化重试的惩罚参数提升量（design.md D7：1.05 → 1.3 一类）
_REPETITION_BOOST = 0.25
_REPETITION_CAP = 1.5
_PRESENCE_BOOST = 0.5
_PRESENCE_CAP = 2.0

# 失败占位（保持管线活性，不清空既有字幕状态）
FAILURE_PLACEHOLDER = '[翻译失败]'

# 热身翻译探针（spawn 后验证端到端可用，避免"健康检查通过但推理不可用"）
_WARMUP_TEXT = '你好'
_WARMUP_TARGET_NAME = '英语'
_WARMUP_MAX_TOKENS = 32

# 输出净化 / 退化检测
_FENCE_EDGE = '```'
_PREFIX_RE = re.compile(
    r'^(translation|translated text|译文|翻译)\s*[:：]\s*', re.IGNORECASE
)
_ECHO_MARKERS = (
    '将以下文本翻译为', '只需要输出翻译后的结果', '不要额外解释',
    '只输出翻译结果', '不要任何解释', 'translate the following',
)
_REPEAT_NGRAM = 8
_REPEAT_LIMIT = 3

# 流式渐进推送（design.md D2/D6）：持有缓冲 / 终止标点 / 节流阈值
# 均为模块常量，测试经 monkeypatch 注入；函数内经模块作用域引用以支持覆盖
_STREAM_HOLD_BACK_CHARS = 8
_STREAM_SENTENCE_END = '。！？…\n'
_STREAM_MIN_INTERVAL_MS = 50
_STREAM_MIN_CHARS = 2

# 旧 config.yaml 键（design.md D9：一次性忽略，不重写用户文件）
_LEGACY_KEYS = ('primary_model', 'fallback_model', 'lazy_load', 'preload_primary')


class TranslationDegenerateError(RuntimeError):
    """净化与重试后仍判定为退化输出（不计入引擎健康失败，走失败占位）"""


class _StreamRepeatAbort(Exception):
    """流式生成中检测到复读循环的内部中止信号（退出流上下文即关闭连接）"""


def _has_repeat_loop(text: str) -> bool:
    """重复 n-gram 循环检测（同一 8-gram 出现 ≥3 次）"""
    if len(text) < _REPEAT_NGRAM * _REPEAT_LIMIT:
        return False
    seen: Dict[str, int] = {}
    step = max(1, _REPEAT_NGRAM // 2)
    for i in range(0, len(text) - _REPEAT_NGRAM + 1, step):
        gram = text[i:i + _REPEAT_NGRAM]
        seen[gram] = seen.get(gram, 0) + 1
        if seen[gram] >= _REPEAT_LIMIT:
            return True
    return False


def _looks_like_instruction_echo(text: str) -> bool:
    """指令回显检测（输出混入 prompt 指令文本）"""
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _ECHO_MARKERS)


def _extract_tps(data: dict) -> Optional[float]:
    """从 llama-server 响应提取本次生成速度（tok/s）；缺失或非法值返回 None"""
    timings = data.get('timings')
    if not isinstance(timings, dict):
        return None
    value = timings.get('predicted_per_second')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


class Translator:
    """翻译引擎：llama-server sidecar 的生命周期与请求编排"""

    def __init__(
        self,
        config: dict,
        *,
        manager: Optional[LlamaServerManager] = None,
        client: Optional[httpx.AsyncClient] = None,
        cache_root=None,
    ):
        self.config = config.get('translation', {}) or {}

        # 设备偏好语义与 ASR 保持一致（get_model_info 的 device 字段）
        self.device = normalize_device(self.config.get('device', 'auto'))

        # 目标语言与当前模型（注册表校验；未知默认值回落内置默认并告警）
        self.target_languages: list = self.config.get('target_languages', ['zh', 'en'])
        self._registry: Dict[str, TranslationModel] = self._build_registry()
        configured_model = self.config.get('default_model', DEFAULT_MODEL_ID)
        model = self._registry.get(configured_model)
        if model is None:
            logger.warning(
                "config.yaml 的 translation.default_model 不在注册表内: %r，回落到 %s",
                configured_model, DEFAULT_MODEL_ID,
            )
            model = default_model()
        self._model: TranslationModel = model
        self._profile = get_profile(model.profile)

        # 请求/进程参数
        download_cfg = self.config.get('download')
        self.download_source = (
            download_cfg.get('source', 'auto')
            if isinstance(download_cfg, dict) else 'auto'
        )
        self.n_ctx = int(self.config.get('n_ctx', 4096) or 4096)
        self.timeout_s = float(self.config.get('timeout_s', 30) or 30)
        # 流式输出开关（design.md D8：声明=真相，缺失默认开；false 走整段路径）
        self._stream_enabled = bool(self.config.get('stream', True))
        self.cache_root = cache_root  # None → translation_models 默认缓存根

        for key in _LEGACY_KEYS:
            if key in self.config:
                logger.warning(
                    "检测到旧翻译配置键 translation.%s（已废弃，本版本忽略）", key
                )

        # 状态
        self._initialized = False
        self._switching = False
        self._resolved_device: Optional[str] = None
        self._device_reason: Optional[str] = None
        self._probe: Optional[dict] = None
        self._switch_lock: Optional[asyncio.Lock] = None  # 延迟创建（需事件循环）
        self._download_progress_callback: Optional[DownloadProgressCallback] = None
        self._health_callback: Optional[HealthCallback] = None

        # 运行期健壮性状态（与 ASR 侧语义对称）
        self._runtime_degraded = False
        self._consecutive_failures = 0
        self._persistent_failure_notified = False

        # llama-server 进程管理（可注入 fake；kill/重启/健康检查由其负责）
        self._manager: LlamaServerManager = manager or LlamaServerManager(
            on_event=self._on_server_event
        )
        self._client = client  # httpx.AsyncClient（懒创建；测试注入 MockTransport）

    # ------------------------------------------------------------------ #
    # 注册表（内置 + config.yaml 增补）
    # ------------------------------------------------------------------ #

    def _build_registry(self) -> Dict[str, TranslationModel]:
        """内置注册表 + config.yaml `models` 增补（无效条目忽略并告警）"""
        registry: Dict[str, TranslationModel] = {m.id: m for m in REGISTRY}
        extras = self.config.get('models')
        if not isinstance(extras, list):
            return registry
        for entry in extras:
            if not isinstance(entry, dict):
                logger.warning("config.yaml 翻译模型增补条目非字典，已忽略: %r", entry)
                continue
            try:
                model = TranslationModel(
                    id=entry['id'],
                    display_name=entry.get('display_name', entry['id']),
                    repo=entry['repo'],
                    revision=entry.get('revision', 'main'),
                    filename=entry['filename'],
                    size_bytes=int(entry['size_bytes']),
                    sha256=entry['sha256'],
                    license=entry.get('license', 'unknown'),
                    license_note=entry.get('license_note', ''),
                    profile=entry.get('profile', 'hy-mt2'),
                    modelscope_repo=entry.get('modelscope_repo'),
                )
                get_profile(model.profile)  # profile 引用校验
            except Exception as e:  # noqa: BLE001 - 增补条目无效不阻断
                logger.warning("config.yaml 翻译模型增补条目无效，已忽略: %r (%s)", entry, e)
                continue
            if model.id in registry:
                logger.warning("翻译模型 id 与内置注册表冲突，忽略增补条目: %s", model.id)
                continue
            registry[model.id] = model
        return registry

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def is_ready(self) -> bool:
        """翻译服务可用：已初始化、不在切换中、llama-server 健康就绪"""
        return (
            self._initialized
            and not self._switching
            and self._manager.is_ready
        )

    @property
    def resolved_device(self) -> Optional[str]:
        """实际使用设备（'cuda' | 'cpu' | None=尚未解析）"""
        return self._resolved_device

    @property
    def device_reason(self) -> Optional[str]:
        """设备选择/降级原因（auto | user | no_cuda | load_failed | runtime_failed | None）"""
        return self._device_reason

    def set_download_progress_callback(self, callback: DownloadProgressCallback):
        """设置模型下载进度回调（形状：model_name/progress/message）"""
        self._download_progress_callback = callback

    def set_health_callback(self, callback: Optional[HealthCallback]):
        """
        设置健康事件回调（运行期降级 / 连续失败）。

        事件形状（与 ASR 侧一致，多携带 model_id）：
        - {'event':'runtime_degraded','resolved':'cpu','reason':'runtime_failed','model_id':str}
        - {'event':'persistent_failure','failures':int,'last_error':str,'model_id':str}
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
            logger.exception("翻译健康回调异常")

    def _report_progress(self, model_name: str, progress: float, message: str):
        """报告下载进度"""
        if self._download_progress_callback:
            self._download_progress_callback(model_name, progress, message)
        logger.info(f"[{model_name}] {progress:.1f}% - {message}")

    def _get_switch_lock(self) -> asyncio.Lock:
        """获取串行化锁（懒创建，须在事件循环线程调用）"""
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        return self._switch_lock

    def _get_client(self) -> httpx.AsyncClient:
        """获取翻译请求客户端（懒创建；连接 5s / 读取 timeout_s）"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_s, connect=5.0)
            )
        return self._client

    # ------------------------------------------------------------------ #
    # 设备解析与进程拉起
    # ------------------------------------------------------------------ #

    def _detect_device(self, load_result: Optional[bool] = None,
                       probe: Optional[dict] = None) -> dict:
        """检测并决策翻译设备（经 device_support 的 llama.cpp 探针）"""
        if probe is None:
            probe = probe_compute()['translation']
        self._probe = probe
        return decide_device(self.device, probe, load_result=load_result)

    def _resolve_device(self) -> str:
        """以 llama.cpp 探针解析并缓存翻译设备（不发起拉起尝试）"""
        if self._resolved_device is not None:
            return self._resolved_device

        decision = self._detect_device()
        self._resolved_device = decision['resolved']
        self._device_reason = decision['reason']

        if self._resolved_device == 'cpu' and self.device != 'cpu':
            logger.warning(
                "翻译使用 CPU（reason=%s）；偏好=%s，探针: %s",
                decision['reason'], self.device,
                (self._probe or {}).get('detail'),
            )
        else:
            logger.info(
                "翻译设备: %s（reason=%s）",
                self._resolved_device, self._device_reason,
            )
        return self._resolved_device

    async def _ensure_model_file(self, model: TranslationModel):
        """确保模型文件就绪（缺失先下载，进度经回调可见）"""
        path = model_path(model, self.cache_root)
        if is_downloaded(model, cache_root=self.cache_root):
            return path
        self._report_progress(model.display_name, 0.0, '模型未下载，开始下载…')
        return await download_model(
            model,
            source=self.download_source,
            progress=self._report_progress,
            cache_root=self.cache_root,
        )

    async def _start_server(self, device: str):
        """按设备拉起 llama-server 并执行热身翻译（失败清理并抛错）"""
        exe = resolve_device_binary(None, device)
        if not exe.exists():
            raise RuntimeError(f"llama-server 二进制缺失（{device}）: {exe}")
        config = ServerConfig(
            exe_path=exe,
            model_path=model_path(self._model, self.cache_root),
            n_ctx=self.n_ctx,
            n_gpu_layers=99 if device == 'cuda' else 0,
            extra_args=self._profile.server_extra_args,
        )
        await self._manager.start(config)
        await self._warmup()

    async def _warmup(self):
        """spawn 后热身翻译：验证健康检查之外的真实推理链路可用。

        流式模式下 MUST 走与生产一致的 SSE 链路：stream=true 的响应体是
        SSE 帧（以 "data: " 开头），用非流式解析会失败并被误判为"拉起失败"
        而错误降级 CPU（回归修复：add-llm-streaming-output 真机实测发现）。
        """
        try:
            if self._stream_enabled:
                await self._request_chat_stream(
                    _WARMUP_TEXT, _WARMUP_TARGET_NAME,
                    max_tokens=_WARMUP_MAX_TOKENS,
                )
            else:
                await self._request_chat(
                    _WARMUP_TEXT, _WARMUP_TARGET_NAME, max_tokens=_WARMUP_MAX_TOKENS
                )
        except Exception as e:  # noqa: BLE001 - 统一转为热身失败
            raise RuntimeError(f"翻译服务热身失败: {e}") from e

    async def _load_with_fallback(self):
        """
        统一拉起路径：探针决策 → 尝试 GPU → 拉起/热身失败静默降级 CPU 构建
        （reason=load_failed）；已解析设备的重启路径按既有决策直连。
        """
        if self._resolved_device is None:
            probe = probe_compute()['translation']
            decision = self._detect_device(probe=probe)
        else:
            decision = {
                'resolved': self._resolved_device,
                'reason': self._device_reason or 'auto',
                'attempt_cuda': self._resolved_device == 'cuda',
            }

        if decision['attempt_cuda']:
            try:
                await self._start_server('cuda')
            except Exception as e:  # noqa: BLE001 - GPU 拉起失败静默降级，不冒泡
                logger.warning(
                    "翻译 GPU 拉起失败，静默降级 CPU 构建"
                    "（偏好=%s，reason=load_failed，探针=%s）：%s",
                    self.device, (self._probe or {}).get('detail'), e,
                )
                await self._start_server('cpu')
                self._resolved_device = 'cpu'
                self._device_reason = 'load_failed'
            else:
                self._resolved_device = 'cuda'
                self._device_reason = decision['reason']
                logger.info(
                    "翻译使用 CUDA（reason=%s）；探针: %s",
                    self._device_reason, (self._probe or {}).get('detail'),
                )
            return

        # CPU 路径：no_cuda / 显式 cpu / 已降级
        await self._start_server('cpu')
        self._resolved_device = 'cpu'
        self._device_reason = decision['reason']
        if decision['reason'] == 'no_cuda' and self.device != 'cpu':
            logger.warning(
                "翻译未检测到可用 CUDA，使用 CPU 构建（偏好=%s）", self.device
            )
        else:
            logger.info("翻译使用 CPU 构建（reason=%s）", self._device_reason)

    async def _ensure_ready_locked(self):
        """锁内确保当前模型 server 就绪（文件就绪 → 拉起（含降级）→ 热身）"""
        model = self._model
        if (
            self._manager.is_ready
            and self._manager.model_path == model_path(model, self.cache_root)
        ):
            return
        await self._ensure_model_file(model)
        await self._load_with_fallback()

    async def initialize(self):
        """
        初始化翻译引擎：解析设备（不拉起进程）。

        llama-server 由启动后的后台任务 `ensure_default()` 预载；
        首次使用时若仍未就绪由调用方按需重试。
        """
        if self._initialized:
            return
        async with self._get_switch_lock():
            if self._initialized:
                return
            self._resolve_device()
            self._initialized = True
            logger.info(
                "翻译引擎初始化完成（模型 %s，偏好 %s，设备解析 %s/%s）",
                self._model.id, self.device,
                self._resolved_device, self._device_reason,
            )

    async def ensure_default(self):
        """
        确保默认（当前）模型 server 就绪：缺失先下载（进度可见），再拉起并热身。

        幂等且锁串行化（启动预载与首次使用重试共用）；失败向上抛出，
        由调用方决定转后台重试或回执。
        """
        async with self._get_switch_lock():
            await self._ensure_ready_locked()

    async def stop(self):
        """终止 llama-server 并关闭 HTTP 客户端（幂等）"""
        await self._manager.stop()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不影响停止语义
                logger.debug("关闭翻译 HTTP 客户端失败（忽略）", exc_info=True)
            self._client = None

    # ------------------------------------------------------------------ #
    # llama-server 事件
    # ------------------------------------------------------------------ #

    def _on_server_event(self, kind: str, detail: Dict):
        """进程事件：重启达上限 → GPU 降级重试 / CPU 持续失败告警"""
        if kind == 'gave_up':
            logger.error("llama-server 重启达上限，翻译服务不可用")
            if self._resolved_device == 'cuda' and not self._runtime_degraded:
                self._schedule_degrade()
            else:
                self._emit_health({
                    'event': 'persistent_failure',
                    'failures': self._consecutive_failures,
                    'last_error': 'llama-server 重启次数达上限',
                    'model_id': self._model.id,
                })
        elif kind == 'exit':
            logger.warning("llama-server 异常退出（%s），自动重启中", detail)
        else:
            logger.debug("llama-server 事件 %s: %s", kind, detail)

    # ------------------------------------------------------------------ #
    # 运行期失败记录与一次性降级
    # ------------------------------------------------------------------ #

    def _record_runtime_failure(self, detail: str):
        """记录一次运行期翻译失败：cuda → 触发一次性降级；cpu → 连续失败计数。"""
        self._consecutive_failures += 1

        if self._resolved_device == 'cuda' and not self._runtime_degraded:
            logger.warning("翻译运行期失败（cuda）：%s → 触发一次性降级", detail)
            self._schedule_degrade()
            return

        if (
            self._resolved_device == 'cpu'
            and self._consecutive_failures >= PERSISTENT_FAILURE_THRESHOLD
            and not self._persistent_failure_notified
        ):
            self._persistent_failure_notified = True
            logger.error(
                "翻译 CPU 连续失败 %d 次：%s", self._consecutive_failures, detail
            )
            self._emit_health({
                'event': 'persistent_failure',
                'failures': self._consecutive_failures,
                'last_error': detail,
                'model_id': self._model.id,
            })

    def _schedule_degrade(self):
        """调度一次性运行期降级（须在事件循环上下文；无循环时仅记日志）。"""
        if self._runtime_degraded or self._switching:
            return
        try:
            asyncio.create_task(self._degrade_runtime_to_cpu())
        except RuntimeError:
            logger.warning("无运行中的事件循环，无法调度翻译运行期降级")

    async def _degrade_runtime_to_cpu(self):
        """
        运行期降级：终止当前 server，以 CPU 构建重启（reason=runtime_failed）。

        一次性 + 防抖；失败时保持可恢复（下次 ensure 按 CPU 重试），不冒泡。
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
                    "翻译运行期不可用，降级 CPU 构建重启（reason=runtime_failed）"
                )
                await self._manager.stop()
                await self._ensure_model_file(self._model)
                await self._start_server('cpu')
                self._resolved_device = 'cpu'
                self._device_reason = 'runtime_failed'
                self._runtime_degraded = True
                self._consecutive_failures = 0
                self._persistent_failure_notified = False
                logger.info("翻译运行期降级完成：cpu/runtime_failed")
            except Exception as e:  # noqa: BLE001 - 降级失败不冒泡
                logger.error("翻译运行期降级失败: %s", e)
                # 保持可恢复：记录为 CPU 目标，下次 ensure 按 CPU 重试
                self._resolved_device = 'cpu'
                self._device_reason = 'runtime_failed'
                return
            finally:
                self._switching = False

        self._emit_health({
            'event': 'runtime_degraded',
            'resolved': 'cpu',
            'reason': 'runtime_failed',
            'model_id': self._model.id,
        })

    # ------------------------------------------------------------------ #
    # 请求构造、净化与退化检测
    # ------------------------------------------------------------------ #

    def _max_tokens_for(self, text: str) -> int:
        """max_tokens 动态上限：约 2×输入长度 + 余量，且不超过 profile 硬上限"""
        return max(64, min(self._profile.max_tokens_cap, int(len(text) * 2) + 32))

    def _build_payload(self, text: str, target_name: str,
                       sampling: Optional[dict], max_tokens: int) -> dict:
        """按 profile 构造 /v1/chat/completions 请求体（目标语言名经显式映射注入）"""
        params = dict(self._profile.sampling)
        if sampling:
            params.update(sampling)
        payload = {
            'model': self._model.id,  # llama-server 单模型模式不校验取值
            'messages': build_messages(self._profile, text, target_name),
            'stream': self._stream_enabled,
            'max_tokens': max_tokens,
            **params,
        }
        if self._profile.chat_template_kwargs:
            payload['chat_template_kwargs'] = dict(self._profile.chat_template_kwargs)
        return payload

    async def _request_chat(self, text: str, target_name: str, *,
                            sampling: Optional[dict] = None,
                            max_tokens: Optional[int] = None
                            ) -> Tuple[str, Optional[str], Optional[float]]:
        """
        发起一次 chat 补全请求。

        Returns:
            (原始输出内容, finish_reason, tps)；tps 为该次生成的解码速度
            （tok/s，来自 llama-server 响应 timings），缺失或非法时为 None

        Raises:
            httpx.HTTPError: 连接/超时/HTTP 状态异常（由调用方记失败并回占位）
        """
        payload = self._build_payload(
            text, target_name, sampling, max_tokens or self._max_tokens_for(text)
        )
        client = self._get_client()
        resp = await client.post(
            f"http://127.0.0.1:{self._manager.port}/v1/chat/completions",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        choice = (data.get('choices') or [{}])[0]
        content = (choice.get('message') or {}).get('content') or ''
        return content, choice.get('finish_reason'), _extract_tps(data)

    async def _request_chat_stream(
        self, text: str, target_name: str, *,
        sampling: Optional[dict] = None,
        max_tokens: Optional[int] = None,
        on_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Tuple[str, Optional[str], Optional[float]]:
        """
        流式发起一次 chat 补全请求（SSE 逐帧消费，design.md D1）。

        返回形状与 _request_chat 对齐：(累积原始输出, finish_reason, tps)。
        - 跳过空行与 `:` 开头的保活注释行；`data: [DONE]` 终止（先于 JSON 判定）；
        - 单帧 JSON 解析失败跳过该帧，不中断整个流；
        - 仅累积 `choices[0].delta.content`（首帧 role/null content 忽略）；
        - 每累积一个 content 增量回调 on_delta(累积原文)（供渐进净化推送）；
        - tps 取自最后成功解析帧的 timings（老构建缺失时为 None）。

        Raises:
            httpx.HTTPError: 连接/超时/HTTP 状态异常（由调用方记失败并回占位）
            _StreamRepeatAbort: on_delta 主动抛出；不在此捕获，退出 async with
                即关闭连接（llama-server 检测断连后中止生成）
        """
        payload = self._build_payload(
            text, target_name, sampling, max_tokens or self._max_tokens_for(text)
        )
        client = self._get_client()
        accumulated = ''
        finish_reason: Optional[str] = None
        last_frame: Optional[dict] = None
        async with client.stream(
            "POST",
            f"http://127.0.0.1:{self._manager.port}/v1/chat/completions",
            json=payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.strip():
                    continue  # SSE 事件分隔空行
                if line.startswith(':'):
                    continue  # 保活注释行（静默期 ping）
                if not line.startswith('data:'):
                    continue  # 未知 SSE 字段（event/id/retry）忽略
                data_str = line[len('data:'):].strip()
                if data_str == '[DONE]':
                    break  # 终止（非 JSON，先于 json.loads 判定）
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    logger.debug("跳过无法解析的流式帧: %r", data_str)
                    continue
                if not isinstance(data, dict):
                    logger.debug("跳过非对象流式帧: %r", data_str)
                    continue  # 合法 JSON 但非对象的病态帧：同样跳过不中断
                last_frame = data
                choice = (data.get('choices') or [{}])[0]
                delta = choice.get('delta') or {}
                content = delta.get('content')
                if isinstance(content, str) and content:
                    accumulated += content
                    if on_delta is not None:
                        await on_delta(accumulated)
                frame_finish = choice.get('finish_reason')
                if frame_finish is not None:
                    finish_reason = frame_finish
        tps = _extract_tps(last_frame) if last_frame is not None else None
        return accumulated, finish_reason, tps

    @staticmethod
    def _purify(text: str) -> str:
        """输出净化：剥 markdown 围栏、首尾引号、"Translation:" 类前缀并 trim"""
        s = (text or '').strip()

        # markdown 围栏（可能带语言标注；容忍多行内容）
        if s.startswith(_FENCE_EDGE):
            lines = s.split('\n')
            lines = lines[1:]
            if lines and lines[-1].strip() == _FENCE_EDGE:
                lines = lines[:-1]
            s = '\n'.join(lines).strip()

        # "Translation:" / "译文：" 类前缀（剥引号前后各判一次，覆盖两种包裹顺序）
        match = _PREFIX_RE.match(s)
        if match:
            s = s[match.end():].strip()

        # 首尾引号（中英文常见引号）
        s = s.strip('"\'“”「」『』')

        match = _PREFIX_RE.match(s)
        if match:
            s = s[match.end():].strip()

        return s

    @staticmethod
    def _boost_penalties(sampling: dict) -> dict:
        """退化重试的惩罚提升（提高 repetition/presence penalty）"""
        boosted = dict(sampling)
        if 'repetition_penalty' in boosted:
            boosted['repetition_penalty'] = min(
                _REPETITION_CAP, boosted['repetition_penalty'] + _REPETITION_BOOST
            )
        if 'presence_penalty' in boosted:
            boosted['presence_penalty'] = min(
                _PRESENCE_CAP, boosted['presence_penalty'] + _PRESENCE_BOOST
            )
        if ('repetition_penalty' not in boosted
                and 'presence_penalty' not in boosted):
            boosted['repetition_penalty'] = 1.3
        return boosted

    def _degenerate_reason(self, text: str, finish_reason: Optional[str],
                           source_text: str) -> Optional[str]:
        """退化判定：返回原因字符串；None 表示输出正常"""
        if finish_reason == 'length':
            return '输出触顶 max_tokens'
        if not text:
            return '空输出'
        if _looks_like_instruction_echo(text):
            return '指令回显'
        limit = max(120, len(source_text) * 3)
        if len(text) > limit:
            return f'异常长输出（{len(text)} > {limit}）'
        if _has_repeat_loop(text):
            return '重复 n-gram 循环'
        return None

    async def _translate_single(
        self, text: str, target_name: str
    ) -> Tuple[str, Optional[float]]:
        """
        翻译到单个目标语言：净化 + 退化重试一次（提升惩罚参数）。

        Returns:
            (净化后译文, tps)；tps 为最终成功那一次生成的解码速度，缺失时为 None

        Raises:
            TranslationDegenerateError: 净化与重试后仍判定退化
            httpx.HTTPError: 请求超时/连接/状态异常
        """
        last_reason = ''
        for attempt in range(2):
            sampling = self._boost_penalties(self._profile.sampling) if attempt else None
            raw, finish_reason, tps = await self._request_chat(
                text, target_name, sampling=sampling
            )
            purified = self._purify(raw)
            reason = self._degenerate_reason(purified, finish_reason, text)
            if reason is None:
                # 成功即清零连续失败计数
                self._consecutive_failures = 0
                self._persistent_failure_notified = False
                return purified, tps
            last_reason = reason
            logger.warning(
                "翻译输出退化（%s），提升惩罚参数重试一次", reason
            )
        raise TranslationDegenerateError(f"输出退化: {last_reason}")

    async def _translate_single_stream(
        self, text: str, target_name: str,
        on_partial: Callable[[str, bool], Awaitable[None]],
    ) -> Tuple[str, Optional[float]]:
        """
        流式翻译到单个目标语言：渐进净化推送 + 复读前移止损 + 退化重试。

        - 每个 content 增量对累积原文跑 _purify，命中复读循环立即退出流上下文
          （关闭连接中止生成）并进入重试；
        - 持有缓冲：净化文本未达阈值且无终止标点时 MUST NOT 推送；
        - 节流合帧：距上次推送 ≥ _STREAM_MIN_INTERVAL_MS 或新增 ≥ _STREAM_MIN_CHARS；
          释放后首帧、流结束末帧强制推送；
        - 重试同样流式，以同一回调继续推送（前端同标识原地替换 = 可见重写）。

        Returns:
            (净化后译文, tps)；tps 为最终成功那一次生成的解码速度，缺失时为 None

        Raises:
            TranslationDegenerateError: 净化与重试后仍判定退化
            httpx.HTTPError: 请求超时/连接/状态异常
        """
        last_reason = ''
        for attempt in range(2):
            sampling = self._boost_penalties(self._profile.sampling) if attempt else None
            last_pushed_text = ''
            last_push_at = 0.0
            pushed_this_attempt = False

            async def handle_delta(raw_accumulated: str) -> None:
                nonlocal last_pushed_text, last_push_at, pushed_this_attempt
                purified = self._purify(raw_accumulated)
                if _has_repeat_loop(purified):
                    raise _StreamRepeatAbort()  # 前移止损（由本方法捕获后重试）
                if (len(purified) < _STREAM_HOLD_BACK_CHARS
                        and not any(ch in purified for ch in _STREAM_SENTENCE_END)):
                    return  # 持有缓冲未释放：抑制前缀/围栏闪现
                now = time.monotonic()
                overdue = (now - last_push_at) >= _STREAM_MIN_INTERVAL_MS / 1000.0
                grown = (len(purified) - len(last_pushed_text)) >= _STREAM_MIN_CHARS
                if not (overdue or grown):
                    return  # 合帧：未达时间/字符阈值
                is_first = (attempt == 0 and not pushed_this_attempt)
                await on_partial(purified, is_first)
                last_pushed_text = purified
                last_push_at = now
                pushed_this_attempt = True

            try:
                raw, finish_reason, tps = await self._request_chat_stream(
                    text, target_name, sampling=sampling, on_delta=handle_delta
                )
            except _StreamRepeatAbort:
                last_reason = '复读循环（流中中止）'
                logger.warning(
                    "翻译流中检测到复读循环，中止本次生成并重试（attempt=%d）", attempt
                )
                continue

            purified = self._purify(raw)
            released = (len(purified) >= _STREAM_HOLD_BACK_CHARS
                        or any(ch in purified for ch in _STREAM_SENTENCE_END))
            if released and purified != last_pushed_text:
                # 终帧强制推送（定稿前 UI 显示完整文本）
                is_first = (attempt == 0 and not pushed_this_attempt)
                await on_partial(purified, is_first)
                last_pushed_text = purified

            reason = self._degenerate_reason(purified, finish_reason, text)
            if reason is None:
                # 成功即清零连续失败计数（与 _translate_single 语义对齐）
                self._consecutive_failures = 0
                self._persistent_failure_notified = False
                return purified, tps
            last_reason = reason
            logger.warning(
                "翻译输出退化（%s），提升惩罚参数重试一次", reason
            )
        raise TranslationDegenerateError(f"输出退化: {last_reason}")

    # ------------------------------------------------------------------ #
    # 对外翻译
    # ------------------------------------------------------------------ #

    async def translate(
        self,
        text: str,
        source_language: Optional[str] = None,
        targets: Optional[list] = None
    ) -> Dict[str, str]:
        """
        翻译文本到目标语言（LLM 单次调用产出；仅激活语言由管线控制）。

        兼容包装：仅返回翻译结果；需要生成速度指标的调用方请用
        translate_with_metrics。

        Args:
            text: 源文本
            source_language: 源语言代码（必须来自 ASR 识别结果）
            targets: 目标语言列表，None 时使用配置的全部目标语言

        Returns:
            翻译结果字典 {规范化语言代码: 翻译文本}；失败目标为占位串
        """
        results, _ = await self.translate_with_metrics(text, source_language, targets)
        return results

    async def translate_with_metrics(
        self,
        text: str,
        source_language: Optional[str] = None,
        targets: Optional[list] = None,
        on_partial: Optional[Callable[[str, str, bool], Awaitable[None]]] = None,
    ) -> Tuple[Dict[str, str], Dict[str, float]]:
        """
        翻译文本到目标语言，并返回每个成功目标的 LLM 生成速度（tok/s）。

        翻译语义与 translate 完全一致；额外收集 tps 指标：仅"实际生成成功"
        的目标产生指标（未支持语言对/占位串/请求失败均不产生）。

        Args:
            on_partial: 可选流式回调 (规范化目标语言, 净化累积译文, 是否首帧)；
                仅 self._stream_enabled 为 true 且显式提供时推送进行中帧。

        Returns:
            (翻译结果字典 {规范化语言代码: 翻译文本},
             生成速度字典 {规范化语言代码: tok/s})
        """
        if not self._initialized:
            logger.error("翻译引擎未初始化")
            return {}, {}

        if not text or not text.strip():
            return {}, {}

        if not source_language:
            logger.warning("缺少源语言（应由 ASR 提供），跳过翻译")
            return {}, {}

        if not self._manager.is_ready:
            logger.debug("翻译服务未就绪（预载/重启中），语句跳过")
            return {}, {}

        src = normalize_lang(source_language)
        results: Dict[str, str] = {}
        tps_by_lang: Dict[str, float] = {}

        for target_lang in (targets if targets is not None else self.target_languages):
            tgt = normalize_lang(target_lang)
            if not tgt or tgt == src:
                continue
            target_name = to_prompt_language_name(tgt)
            if target_name is None:
                results[tgt] = unsupported_pair_message(src, tgt)
                continue
            try:
                if self._stream_enabled:
                    async def _forward(accumulated: str, is_first: bool,
                                       _tgt: str = tgt) -> None:
                        if on_partial is not None:
                            await on_partial(_tgt, accumulated, is_first)

                    purified, tps = await self._translate_single_stream(
                        text, target_name, _forward
                    )
                else:
                    purified, tps = await self._translate_single(text, target_name)
                results[tgt] = purified
                if tps is not None:
                    tps_by_lang[tgt] = tps
            except TranslationDegenerateError as e:
                logger.warning(f"翻译到 {tgt} 输出退化且重试未恢复: {e}")
                results[tgt] = FAILURE_PLACEHOLDER
            except Exception as e:  # noqa: BLE001 - 单目标失败不拖垮其余目标
                logger.error(f"翻译到 {tgt} 失败: {e}")
                self._record_runtime_failure(str(e))
                results[tgt] = FAILURE_PLACEHOLDER

        return results, tps_by_lang

    # ------------------------------------------------------------------ #
    # 模型 / 设备切换
    # ------------------------------------------------------------------ #

    async def change_llm(self, model_id: str):
        """
        切换翻译模型（锁串行化；切换期间 is_ready=False，语句安全丢弃）。

        同值且就绪时幂等；旧 server 先终止（显存彻底释放），新模型缺失时
        先下载（进度可见），拉起并通过健康检查 + 热身翻译后完成切换。

        Raises:
            ValueError: model_id 不在注册表内（调用方回 invalid_llm）
            RuntimeError: 下载/拉起失败（调用方回执错误）
        """
        model = self._registry.get(model_id) if isinstance(model_id, str) else None
        if model is None:
            raise ValueError(
                f"不支持的翻译模型: {model_id!r}，可选: {sorted(self._registry)}"
            )

        if model.id == self._model.id and self._manager.is_ready:
            logger.info(f"翻译模型已是 {model.id}，无需切换")
            return

        async with self._get_switch_lock():
            # 锁内双检：并发的相同切换请求只生效一次
            if model.id == self._model.id and self._manager.is_ready:
                logger.info(f"翻译模型已是 {model.id}，无需切换")
                return

            self._switching = True
            try:
                if model.id != self._model.id:
                    logger.info(f"切换翻译模型: {self._model.id} -> {model.id}")
                    await self._manager.stop()  # 终止旧 server，释放显存/内存
                    self._model = model
                    self._profile = get_profile(model.profile)
                    # 新模型重新解析设备（与 ASR change_model 的 fresh 决策语义对称）：
                    # 给出一次干净的 GPU 尝试机会，拉起/热身失败仍静默降级 CPU
                    self._resolved_device = None
                    self._device_reason = None
                await self._ensure_ready_locked()

                # 切换/重试成功：重置运行期状态（新模型重新计健康）
                self._runtime_degraded = False
                self._consecutive_failures = 0
                self._persistent_failure_notified = False
                logger.info(f"翻译模型切换完成: {self._model.id}")
            finally:
                self._switching = False

    async def change_device(self, device: str):
        """
        切换翻译设备偏好：终止当前 server，重置解析缓存并按新设备重新拉起
        （切换前已就绪时；未就绪则仅重置解析，服务按需再拉）。

        Args:
            device: 'auto' | 'cpu' | 'cuda'

        Raises:
            ValueError: 设备值不受支持
        """
        if device not in VALID_DEVICES:
            raise ValueError(f"不支持的设备: {device}，支持: {VALID_DEVICES}")

        if device == self.device and self._initialized:
            logger.info(f"翻译设备偏好已是 {device}，无需切换")
            return

        async with self._get_switch_lock():
            # 锁内双检：并发的相同切换请求只生效一次
            if device == self.device and self._initialized:
                logger.info(f"翻译设备偏好已是 {device}，无需切换")
                return

            self._switching = True
            try:
                was_ready = self._manager.is_ready
                logger.info(f"切换翻译设备: {self.device} -> {device}")

                self.device = device
                self._resolved_device = None
                self._device_reason = None
                await self._manager.stop()

                if was_ready:
                    # 按新设备重新拉起（走完整决策 + 静默降级路径）
                    await self._ensure_ready_locked()
                else:
                    # 未加载过：仅按新偏好解析（同步可见），服务按需再拉
                    self._resolve_device()
            finally:
                self._switching = False

    # ------------------------------------------------------------------ #
    # 信息查询
    # ------------------------------------------------------------------ #

    def get_model_info(self) -> dict:
        """
        获取当前模型与设备信息。

        device=配置偏好；resolved_device/device_reason=实际结果；
        available_models=注册表摘要（含已下载/当前模型标记）。
        """
        return {
            'model': self._model.id,
            'available_models': [
                {
                    'id': m.id,
                    'display_name': m.display_name,
                    'size_bytes': m.size_bytes,
                    'downloaded': is_downloaded(m, cache_root=self.cache_root),
                    'license': m.license,
                    'current': m.id == self._model.id,
                }
                for m in self._registry.values()
            ],
            'target_languages': self.target_languages,
            'initialized': self._initialized,
            'ready': self.is_ready,
            'device': self.device,
            'resolved_device': self._resolved_device,
            'device_reason': self._device_reason,
        }
