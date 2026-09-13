"""
翻译引擎模块 - 本地翻译模型
"""
import asyncio
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

import torch
from transformers import (
    AutoModelForSeq2SeqLM,
    AutoTokenizer,
    NllbTokenizer,
    AutoModelForSeq2SeqLM
)
from langdetect import detect

logger = logging.getLogger(__name__)

# 模型下载进度回调类型
DownloadProgressCallback = Callable[[str, float, str], None]


class Translator:
    """翻译引擎类"""

    # 语言代码映射 (langdetect -> 模型语言代码)
    LANGUAGE_MAP = {
        'ja': 'jpn_Jpan',  # 日语
        'zh': 'zho_Hans',  # 中文
        'en': 'eng_Latn',  # 英语
        'ko': 'kor_Hang',  # 韩语
        'fr': 'fra_Latn',  # 法语
        'de': 'deu_Latn',  # 德语
        'es': 'spa_Latn',  # 西班牙语
        'ru': 'rus_Cyrl',  # 俄语
        'pt': 'por_Latn',  # 葡萄牙语
        'it': 'ita_Latn',  # 意大利语
    }

    def __init__(self, config: dict):
        self.config = config.get('translation', {})
        self.primary_model_name = self.config.get(
            'primary_model', 'Helsinki-NLP/opus-mt-ja-zh'
        )
        self.fallback_model_name = self.config.get(
            'fallback_model', 'facebook/nllb-200-distilled-600M'
        )
        self.target_languages = self.config.get('target_languages', ['zh', 'en'])
        self.device = self.config.get('device', 'auto')

        # 模型缓存目录
        self.model_cache_dir = Path.home() / '.cache' / 'subtitle-translator' / 'translation'
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)

        self._primary_model = None
        self._primary_tokenizer = None
        self._fallback_model = None
        self._fallback_tokenizer = None
        self._initialized = False
        self._download_progress_callback: Optional[DownloadProgressCallback] = None

    def set_download_progress_callback(self, callback: DownloadProgressCallback):
        """
        设置下载进度回调函数

        Args:
            callback: 回调函数，参数为 (model_name, progress_percent, status_message)
        """
        self._download_progress_callback = callback

    def _report_progress(self, model_name: str, progress: float, message: str):
        """报告下载进度"""
        if self._download_progress_callback:
            self._download_progress_callback(model_name, progress, message)
        logger.info(f"[{model_name}] {progress:.1f}% - {message}")

    def _detect_device(self) -> str:
        """检测可用设备"""
        if self.device == 'auto':
            if torch.cuda.is_available():
                return 'cuda'
            return 'cpu'
        return self.device

    async def initialize(self):
        """初始化翻译模型"""
        if self._initialized:
            return

        device = self._detect_device()
        logger.info(f"正在加载翻译模型，设备: {device}")

        # 加载日中专用模型
        self._report_progress(self.primary_model_name, 0, "准备加载日中翻译模型...")
        logger.info(f"加载主模型: {self.primary_model_name}")
        self._report_progress(self.primary_model_name, 10, f"正在下载模型 (~300MB)...")

        self._primary_tokenizer, self._primary_model = await asyncio.to_thread(
            self._load_model_with_progress,
            self.primary_model_name,
            device,
            'primary'
        )
        self._report_progress(self.primary_model_name, 50, "日中模型加载完成")

        # 加载 NLLB 全语种模型
        self._report_progress(self.fallback_model_name, 0, "准备加载全语种翻译模型...")
        logger.info(f"加载备用模型: {self.fallback_model_name}")
        self._report_progress(self.fallback_model_name, 10, f"正在下载模型 (~1.3GB)...")

        self._fallback_tokenizer, self._fallback_model = await asyncio.to_thread(
            self._load_model_with_progress,
            self.fallback_model_name,
            device,
            'fallback'
        )
        self._report_progress(self.fallback_model_name, 100, "全语种模型加载完成")

        self._initialized = True
        logger.info("翻译模型加载完成")

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

    def detect_language(self, text: str) -> str:
        """
        检测文本语言

        Args:
            text: 输入文本

        Returns:
            语言代码 (如 'ja', 'zh', 'en')
        """
        try:
            lang = detect(text)
            return lang
        except Exception:
            # 默认返回英语
            return 'en'

    async def translate(
        self,
        text: str,
        source_language: Optional[str] = None
    ) -> Dict[str, str]:
        """
        翻译文本到多种语言

        Args:
            text: 源文本
            source_language: 源语言代码 (可选，自动检测)

        Returns:
            翻译结果字典 {语言代码: 翻译文本}
        """
        if not self._initialized:
            logger.error("翻译引擎未初始化")
            return {}

        if not text or not text.strip():
            return {}

        # 自动检测语言
        if source_language is None:
            source_language = self.detect_language(text)

        logger.debug(f"源语言: {source_language}，文本: {text[:50]}...")

        results = {}

        # 并行翻译到所有目标语言
        tasks = []
        for target_lang in self.target_languages:
            if target_lang != source_language:
                task = self._translate_single(text, source_language, target_lang)
                tasks.append((target_lang, task))

        # 执行翻译
        for target_lang, task in tasks:
            try:
                translation = await task
                results[target_lang] = translation
            except Exception as e:
                logger.error(f"翻译到 {target_lang} 失败: {e}")
                results[target_lang] = f"[翻译失败: {e}]"

        return results

    async def _translate_single(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> str:
        """
        翻译到单个目标语言

        Args:
            text: 源文本
            source_lang: 源语言
            target_lang: 目标语言

        Returns:
            翻译结果
        """
        # 如果是日语到中文，使用专用模型
        if source_lang == 'ja' and target_lang == 'zh':
            return await self._translate_with_primary(text, 'ja', 'zh')

        # 其他情况使用 NLLB
        return await self._translate_with_nllb(text, source_lang, target_lang)

    async def _translate_with_primary(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> str:
        """
        使用主模型翻译（日中专用）

        Args:
            text: 源文本
            source_lang: 源语言
            target_lang: 目标语言

        Returns:
            翻译结果
        """
        if self._primary_model is None:
            return "[主模型未加载]"

        try:
            # 在线程中执行翻译
            result = await asyncio.to_thread(
                self._run_translation,
                self._primary_model,
                self._primary_tokenizer,
                text,
                max_length=512
            )
            return result

        except Exception as e:
            logger.error(f"主模型翻译失败: {e}")
            return f"[翻译失败: {e}]"

    async def _translate_with_nllb(
        self,
        text: str,
        source_lang: str,
        target_lang: str
    ) -> str:
        """
        使用 NLLB 模型翻译

        Args:
            text: 源文本
            source_lang: 源语言
            target_lang: 目标语言

        Returns:
            翻译结果
        """
        if self._fallback_model is None:
            return "[NLLB模型未加载]"

        try:
            # 转换语言代码
            src_code = self.LANGUAGE_MAP.get(source_lang, f'{source_lang}_Latn')
            tgt_code = self.LANGUAGE_MAP.get(target_lang, f'{target_lang}_Latn')

            # 在线程中执行翻译
            result = await asyncio.to_thread(
                self._run_nllb_translation,
                self._fallback_model,
                self._fallback_tokenizer,
                text,
                src_code,
                tgt_code,
                max_length=512
            )
            return result

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
        """
        执行翻译（同步）

        Args:
            model: 翻译模型
            tokenizer: 分词器
            text: 源文本
            max_length: 最大长度

        Returns:
            翻译结果
        """
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
        """
        执行 NLLB 翻译（同步）

        Args:
            model: NLLB 模型
            tokenizer: NLLB 分词器
            text: 源文本
            src_lang: 源语言代码
            tgt_lang: 目标语言代码
            max_length: 最大长度

        Returns:
            翻译结果
        """
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
        切换主翻译模型

        Args:
            model_name: 新模型名称
        """
        logger.info(f"切换主模型: {self.primary_model_name} -> {model_name}")
        self.primary_model_name = model_name
        self._initialized = False
        self._primary_model = None
        self._primary_tokenizer = None

        await self.initialize()

    def get_model_info(self) -> dict:
        """获取模型信息"""
        return {
            'primary_model': self.primary_model_name,
            'fallback_model': self.fallback_model_name,
            'target_languages': self.target_languages,
            'initialized': self._initialized,
            'device': self.device
        }
