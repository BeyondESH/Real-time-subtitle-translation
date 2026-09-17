"""
语言代码规范化与翻译模型语言映射

管线中的源语言以 faster-whisper 的识别结果为唯一依据（ISO 639-1），
本模块提供规范化纯函数与 NLLB 显式映射表。未映射语言对安全降级，
绝不动态拼接语言代码。
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 中文等语言变体归一表（键为小写、连字符形式）
_NORMALIZE_MAP: Dict[str, str] = {
    'zh-cn': 'zh',
    'zh-tw': 'zh',
    'zh-hans': 'zh',
    'zh-hant': 'zh',
    'zh-hk': 'zh',
    'zh-mo': 'zh',
    'zh-sg': 'zh',
}

# NLLB-200 显式语言代码映射（ISO 639-1 -> NLLB 代码）
NLLB_LANGUAGE_MAP: Dict[str, str] = {
    'zh': 'zho_Hans',  # 中文（简体）
    'ja': 'jpn_Jpan',  # 日语
    'en': 'eng_Latn',  # 英语
    'ko': 'kor_Hang',  # 韩语
    'fr': 'fra_Latn',  # 法语
    'de': 'deu_Latn',  # 德语
    'es': 'spa_Latn',  # 西班牙语
    'ru': 'rus_Cyrl',  # 俄语
    'pt': 'por_Latn',  # 葡萄牙语
    'it': 'ita_Latn',  # 意大利语
}


def normalize_lang(code: Optional[str]) -> str:
    """
    规范化语言代码。

    - 大小写不敏感，下划线/连字符差异被消化
    - 中文各变体（zh-cn/zh-tw/zh-hans/...）归一为 'zh'

    Args:
        code: 原始语言代码，允许 None/空串

    Returns:
        规范化后的语言代码；输入为空时返回空串
    """
    if not code:
        return ''
    normalized = code.strip().lower().replace('_', '-')
    return _NORMALIZE_MAP.get(normalized, normalized)


def to_nllb_code(lang: str) -> Optional[str]:
    """
    转换为 NLLB 语言代码。仅查显式映射表，不做任何拼接。

    Args:
        lang: 规范化前的语言代码（内部会先 normalize）

    Returns:
        NLLB 语言代码；未映射时返回 None
    """
    normalized = normalize_lang(lang)
    code = NLLB_LANGUAGE_MAP.get(normalized)
    if code is None:
        logger.warning(f"NLLB 未支持的语言代码: {lang}")
    return code


def unsupported_pair_message(source_lang: str, target_lang: str) -> str:
    """未支持语言对的占位提示"""
    return f'[未支持的语言对: {source_lang}→{target_lang}]'
