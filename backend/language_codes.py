"""
语言代码规范化与翻译提示词语言名映射

管线中的源语言以 faster-whisper 的识别结果为唯一依据（ISO 639-1），
本模块提供规范化纯函数与「ISO 码 → prompt 语言显示名」显式映射表。
未映射语言对安全降级，绝不动态拼接语言名或语言代码。
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

# 语言显示名映射（ISO 639-1 -> 提示词中的目标语言名，用于翻译指令注入）
PROMPT_LANGUAGE_NAMES: Dict[str, str] = {
    'zh': '简体中文',
    'ja': '日语',
    'en': '英语',
    'ko': '韩语',
    'fr': '法语',
    'de': '德语',
    'es': '西班牙语',
    'ru': '俄语',
    'pt': '葡萄牙语',
    'it': '意大利语',
    'ar': '阿拉伯语',
    'th': '泰语',
    'vi': '越南语',
    'id': '印度尼西亚语',
    'hi': '印地语',
    'tr': '土耳其语',
    'nl': '荷兰语',
    'pl': '波兰语',
    'sv': '瑞典语',
    'uk': '乌克兰语',
    'ms': '马来语',
    'tl': '菲律宾语',
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


def to_prompt_language_name(lang: str) -> Optional[str]:
    """
    转换为提示词语言显示名。仅查显式映射表，不做任何拼接。

    Args:
        lang: 规范化前的语言代码（内部会先 normalize）

    Returns:
        语言显示名（如 '简体中文'）；未映射时返回 None
    """
    normalized = normalize_lang(lang)
    name = PROMPT_LANGUAGE_NAMES.get(normalized)
    if name is None:
        logger.warning(f"未支持的目标语言代码: {lang}")
    return name


def unsupported_pair_message(source_lang: str, target_lang: str) -> str:
    """未支持语言对的占位提示"""
    return f'[未支持的语言对: {source_lang}→{target_lang}]'
