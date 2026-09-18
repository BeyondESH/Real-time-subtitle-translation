"""
翻译引擎模块 - 本地翻译模型

模型分级加载：日中主模型可后台预载，NLLB 全语种模型按需懒加载。
加载由锁串行化；懒加载期间翻译请求等待同一锁，进度持续上报。
torch/transformers 为函数内延迟导入，模块本身轻量可测。
"""
import asyncio
import logging
from pathlib import Path
from typing import Callable, Dict, Optional

from device_support import (
    VALID_DEVICES,
    decide_device,
    normalize_device,
    probe_compute,
)
from language_codes import (
    NLLB_LANGUAGE_MAP,
    normalize_lang,
    to_nllb_code,
    unsupported_pair_message,
)

logger = logging.getLogger(__name__)

# 模型下载进度回调类型
DownloadProgressCallback = Callable[[str, float, str], None]


class Translator:
    """翻译引擎类"""

    def __init__(self, config: dict):
        self.config = config.get('translation', {})
        self.primary_model_name = self.config.get(
            'primary_model', 'Helsinki-NLP/opus-mt-ja-zh'
        )
        self.fallback_model_name = self.config.get(
            'fallback_model', 'facebook/nllb-200-distilled-600M'
        )
        self.target_languages = self.config.get('target_languages', ['zh', 'en'])
        # self.device 语义保持为【配置偏好】（get_model_info 的 device 字段兼容依赖）
        self.device = normalize_device(self.config.get('device', 'auto'))
        self.lazy_load = self.config.get('lazy_load', True)
        self.preload_primary = self.config.get('preload_primary', True)

        # 模型缓存目录
        self.model_cache_dir = Path.home() / '.cache' / 'subtitle-translator' / 'translation'
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)

        self._primary_model = None
        self._primary_tokenizer = None
        self._fallback_model = None
        self._fallback_tokenizer = None
        self._initialized = False
        self._resolved_device: Optional[str] = None
        self._device_reason: Optional[str] = None
        self._probe: Optional[dict] = None
        self._locks: Dict[str, asyncio.Lock] = {}  # 延迟创建（需在事件循环线程）
        self._download_progress_callback: Optional[DownloadProgressCallback] = None

    def set_download_progress_callback(self, callback: DownloadProgressCallback):
        """
        设置下载进度回调函数

        Args:
            callback: 回调函数，参数为 (model_name, progress_percent, status_message)
        """
        self._download_progress_callback = callback

    def _report_progress(self, model_name: str, progress: float, message: str):
        """报告下载进度（可能被 worker 线程触发，回调方需自行保证线程安全）"""
        if self._download_progress_callback:
            self._download_progress_callback(model_name, progress, message)
        logger.info(f"[{model_name}] {progress:.1f}% - {message}")

    def _get_lock(self, name: str) -> asyncio.Lock:
        """获取指定模型的加载锁（延迟创建，须在事件循环线程调用）"""
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock

    @property
    def resolved_device(self) -> Optional[str]:
        """实际使用设备（'cuda' | 'cpu' | None=尚未解析）"""
        return self._resolved_device

    @property
    def device_reason(self) -> Optional[str]:
        """设备选择原因（auto | user | no_cuda | load_failed | None）"""
        return self._device_reason

    def _resolve_device(self) -> str:
        """以 torch 探针解析并缓存翻译设备，同时记录选择/降级原因"""
        if self._resolved_device is not None:
            return self._resolved_device

        probe = probe_compute()['translation']
        self._probe = probe
        decision = decide_device(self.device, probe)
        self._resolved_device = decision['resolved']
        self._device_reason = decision['reason']

        if self._resolved_device == 'cpu' and self.device != 'cpu':
            logger.warning(
                "翻译使用 CPU（reason=%s）；偏好=%s，探针: %s",
                decision['reason'], self.device, probe.get('detail'),
            )
        else:
            logger.info(
                "翻译设备: %s（reason=%s）",
                self._resolved_device, self._device_reason,
            )
        return self._resolved_device

    async def initialize(self):
        """
        初始化翻译引擎。

        lazy_load=True（默认）：只解析设备，模型按需加载；
        lazy_load=False：立即加载全部模型（兼容旧行为）。
        """
        if self._initialized:
            return

        self._resolve_device()

        if not self.lazy_load:
            await self.ensure_primary()
            await self.ensure_nllb()

        self._initialized = True
        logger.info("翻译引擎初始化完成")

    async def ensure_primary(self):
        """确保日中主模型已加载（锁串行化，可重入）"""
        if self._primary_model is not None:
            return
        async with self._get_lock('primary'):
            if self._primary_model is not None:
                return
            self._report_progress(self.primary_model_name, 0, "准备加载日中翻译模型...")
            self._primary_tokenizer, self._primary_model = await asyncio.to_thread(
                self._load_model_with_progress,
                self.primary_model_name,
                self._resolve_device(),
                'primary'
            )

    async def ensure_nllb(self):
        """确保 NLLB 全语种模型已加载（锁串行化，可重入）"""
        if self._fallback_model is not None:
            return
        async with self._get_lock('nllb'):
            if self._fallback_model is not None:
                return
            self._report_progress(self.fallback_model_name, 0, "准备加载全语种翻译模型...")
            self._fallback_tokenizer, self._fallback_model = await asyncio.to_thread(
                self._load_model_with_progress,
                self.fallback_model_name,
                self._resolve_device(),
                'fallback'
            )

    @property
    def primary_loaded(self) -> bool:
        return self._primary_model is not None

    @property
    def nllb_loaded(self) -> bool:
        return self._fallback_model is not None

    def _load_model_with_progress(self, model_name: str, device: str, model_type: str):
        """
        加载模型（同步，在线程中运行）

        Args:
            model_name: 模型名称
            device: 计算设备
            model_type: 模型类型 (primary/fallback)

        Returns:
            (tokenizer, model) 元组
        """
        try:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            # 设置缓存目录
            cache_dir = str(self.model_cache_dir / model_type)

            self._report_progress(model_name, 20, "正在下载分词器...")

            # 加载分词器
            tokenizer = AutoTokenizer.from_pretrained(
                model_name,
                cache_dir=cache_dir
            )

            self._report_progress(model_name, 40, "正在下载模型权重...")

            # 加载模型
            model = AutoModelForSeq2SeqLM.from_pretrained(
                model_name,
                cache_dir=cache_dir
            )

            self._report_progress(model_name, 80, "正在初始化模型...")

            if device == 'cuda':
                model = model.cuda()

            model.eval()

            self._report_progress(model_name, 100, "模型加载完成")
            return tokenizer, model

        except Exception as e:
            logger.error(f"加载模型失败 {model_name}: {e}")
            self._report_progress(model_name, 0, f"加载失败: {e}")
            raise

    async def translate(
        self,
        text: str,
        source_language: Optional[str] = None,
        targets: Optional[list] = None
    ) -> Dict[str, str]:
        """
        翻译文本到目标语言

        Args:
            text: 源文本
            source_language: 源语言代码（必须来自 ASR 识别结果）
            targets: 目标语言列表，None 时使用配置的全部目标语言

        Returns:
            翻译结果字典 {规范化语言代码: 翻译文本}
        """
        if not self._initialized:
            logger.error("翻译引擎未初始化")
            return {}

        if not text or not text.strip():
            return {}

        if not source_language:
            logger.warning("缺少源语言（应由 ASR 提供），跳过翻译")
            return {}

        src = normalize_lang(source_language)
        logger.debug(f"源语言: {src}，文本: {text[:50]}...")

        results = {}

        for target_lang in (targets if targets is not None else self.target_languages):
            tgt = normalize_lang(target_lang)
            if not tgt or tgt == src:
                continue
            try:
                results[tgt] = await self._translate_single(text, src, tgt)
            except Exception as e:
                logger.error(f"翻译到 {tgt} 失败: {e}")
                results[tgt] = f"[翻译失败: {e}]"

        return results

    async def _translate_single(self, text: str, src: str, tgt: str) -> str:
        """
        翻译到单个目标语言

        日语→中文走专用模型；其余语言对走 NLLB（未映射语言对返回占位串）。
        """
        # 日语到中文，使用专用模型
        if src == 'ja' and tgt == 'zh':
            await self.ensure_primary()
            return await self._translate_with_primary(text)

        # 其他语言对使用 NLLB
        src_code = to_nllb_code(src)
        tgt_code = to_nllb_code(tgt)
        if not src_code or not tgt_code:
            return unsupported_pair_message(src, tgt)

        await self.ensure_nllb()
        return await self._translate_with_nllb(text, src_code, tgt_code)

    async def _translate_with_primary(self, text: str) -> str:
        """使用主模型翻译（日中专用）"""
        try:
            return await asyncio.to_thread(
                self._run_translation,
                self._primary_model,
                self._primary_tokenizer,
                text,
                512
            )
        except Exception as e:
            logger.error(f"主模型翻译失败: {e}")
            return f"[翻译失败: {e}]"

    async def _translate_with_nllb(self, text: str, src_code: str, tgt_code: str) -> str:
        """使用 NLLB 模型翻译"""
        try:
            return await asyncio.to_thread(
                self._run_nllb_translation,
                self._fallback_model,
                self._fallback_tokenizer,
                text,
                src_code,
                tgt_code,
                512
            )
        except Exception as e:
            logger.error(f"NLLB翻译失败: {e}")
            return f"[翻译失败: {e}]"

    def _run_translation(
        self,
        model,
        tokenizer,
        text: str,
        max_length: int = 512
    ) -> str:
        """执行翻译（同步，在线程中运行）"""
        import torch

        device = next(model.parameters()).device

        inputs = tokenizer(text, return_tensors="pt", padding=True)
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_length=max_length,
                num_beams=4,
                early_stopping=True
            )

        result = tokenizer.decode(outputs[0], skip_special_tokens=True)
        return result

    def _run_nllb_translation(
        self,
        model,
        tokenizer,
        text: str,
        src_lang: str,
        tgt_lang: str,
        max_length: int = 512
    ) -> str:
        """执行 NLLB 翻译（同步，在线程中运行）"""
        import torch

        device = next(model.parameters()).device

        # NLLB 需要特殊处理
        tokenizer.src_lang = src_lang
        inputs = tokenizer(text, return_tensors="pt", padding=True)
        inputs = {k: v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_length=max_length,
                num_beams=4,
                forced_bos_token_id=tokenizer.convert_tokens_to_ids(tgt_lang)
            )

        result = tokenizer.decode(outputs[0], skip_special_tokens=True)
        return result

    async def change_primary_model(self, model_name: str):
        """
        切换主翻译模型（下次用到时懒加载新模型）

        Args:
            model_name: 新模型名称
        """
        logger.info(f"切换主模型: {self.primary_model_name} -> {model_name}")
        async with self._get_lock('primary'):
            self.primary_model_name = model_name
            self._primary_model = None
            self._primary_tokenizer = None

    async def change_device(self, device: str):
        """
        切换翻译设备偏好：重置解析缓存、卸载已加载模型；主模型若原已加载
        则在后台按新设备重新预载（NLLB 保持懒加载）。

        Args:
            device: 'auto' | 'cpu' | 'cuda'

        Raises:
            ValueError: 设备值不受支持
        """
        if device not in VALID_DEVICES:
            raise ValueError(f"不支持的设备: {device}，支持: {VALID_DEVICES}")

        if device == self.device:
            logger.info(f"翻译设备偏好已是 {device}，无需切换")
            return

        logger.info(f"切换翻译设备: {self.device} -> {device}")
        previous_primary_loaded = self._primary_model is not None

        self.device = device
        self._resolved_device = None
        self._device_reason = None

        async with self._get_lock('primary'):
            self._primary_model = None
            self._primary_tokenizer = None
        async with self._get_lock('nllb'):
            self._fallback_model = None
            self._fallback_tokenizer = None

        # 立即按新偏好解析设备（结果同步可见）
        self._resolve_device()

        if previous_primary_loaded and self.preload_primary:
            asyncio.create_task(self._preload_primary_after_switch())

    async def _preload_primary_after_switch(self):
        """设备切换后按新设备后台重新预载主模型，失败降级为懒加载"""
        try:
            await self.ensure_primary()
            logger.info("翻译设备切换后主模型预载完成")
        except Exception as e:
            logger.warning(f"翻译设备切换后主模型预载失败，将在首次使用时重试: {e}")

    def get_model_info(self) -> dict:
        """获取模型信息（device=配置偏好；resolved_device/device_reason=实际结果）"""
        return {
            'primary_model': self.primary_model_name,
            'fallback_model': self.fallback_model_name,
            'target_languages': self.target_languages,
            'initialized': self._initialized,
            'primary_loaded': self.primary_loaded,
            'nllb_loaded': self.nllb_loaded,
            'lazy_load': self.lazy_load,
            'device': self.device,
            'resolved_device': self._resolved_device,
            'device_reason': self._device_reason,
            'nllb_languages': sorted(NLLB_LANGUAGE_MAP.keys()),
        }
