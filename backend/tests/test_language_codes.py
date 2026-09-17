"""
语言代码规范化与翻译语言对映射测试
"""
from unittest.mock import AsyncMock

import pytest

from language_codes import (
    NLLB_LANGUAGE_MAP,
    normalize_lang,
    to_nllb_code,
    unsupported_pair_message,
)
from translator import Translator


class TestNormalizeLang:
    def test_zh_variants(self):
        assert normalize_lang('zh-cn') == 'zh'
        assert normalize_lang('zh-TW') == 'zh'
        assert normalize_lang('ZH') == 'zh'
        assert normalize_lang('zh_hans') == 'zh'
        assert normalize_lang('zh-Hant') == 'zh'

    def test_common_codes_unchanged(self):
        assert normalize_lang('ja') == 'ja'
        assert normalize_lang('en') == 'en'
        assert normalize_lang('ko') == 'ko'

    def test_empty_input(self):
        assert normalize_lang(None) == ''
        assert normalize_lang('') == ''
        assert normalize_lang('  ') == ''


class TestNllbMapping:
    def test_mapped_languages(self):
        assert to_nllb_code('zh') == 'zho_Hans'
        assert to_nllb_code('zh-cn') == 'zho_Hans'  # 变体先归一
        assert to_nllb_code('ja') == 'jpn_Jpan'
        assert to_nllb_code('en') == 'eng_Latn'

    def test_unmapped_returns_none(self):
        assert to_nllb_code('xx') is None
        assert to_nllb_code('') is None

    def test_no_fabricated_codes(self):
        """绝不拼接语言代码（回归 zh-cn_Latn bug）"""
        for lang in NLLB_LANGUAGE_MAP:
            code = to_nllb_code(lang)
            assert '_' in code and code.split('_')[0].isalpha()
        assert to_nllb_code('zh-cn') != 'zh-cn_Latn'

    def test_unsupported_pair_message(self):
        msg = unsupported_pair_message('xx', 'zh')
        assert 'xx' in msg and 'zh' in msg


def make_translator():
    config = {
        'translation': {
            'primary_model': 'Helsinki-NLP/opus-mt-ja-zh',
            'fallback_model': 'facebook/nllb-200-distilled-600M',
            'target_languages': ['zh', 'en'],
            'device': 'cpu',
            'lazy_load': True,
            'preload_primary': False,
        }
    }
    t = Translator(config)
    t._initialized = True
    return t


class TestTranslatorRouting:
    async def test_same_language_skipped(self):
        """源语言与目标语言相同 → 跳过该语言对"""
        t = make_translator()
        t.ensure_nllb = AsyncMock()
        t._translate_with_nllb = AsyncMock(return_value='hello')

        results = await t.translate('你好世界', 'zh-cn')  # 变体归一为 zh
        assert set(results.keys()) == {'en'}
        assert results['en'] == 'hello'

    async def test_unsupported_pair_placeholder(self):
        """未映射语言对返回占位串，不触发模型加载"""
        t = make_translator()
        t.ensure_nllb = AsyncMock()

        results = await t.translate('text', source_language='xx')
        # en 目标可映射，但源语言 xx 未映射 → 占位
        assert '未支持的语言对' in results['en']
        t.ensure_nllb.assert_not_called()

    async def test_ja_zh_uses_primary(self):
        """日→中走主模型，不触碰 NLLB"""
        t = make_translator()
        t.target_languages = ['zh']  # 只留日中语言对
        t.ensure_primary = AsyncMock()
        t.ensure_nllb = AsyncMock()
        t._translate_with_primary = AsyncMock(return_value='你好')

        results = await t.translate('こんにちは', 'ja')
        assert results['zh'] == '你好'
        t.ensure_primary.assert_called_once()
        t.ensure_nllb.assert_not_called()

    async def test_missing_source_language(self):
        """缺少源语言（应来自 ASR）→ 返回空并告警"""
        t = make_translator()
        results = await t.translate('text', None)
        assert results == {}

    async def test_empty_text(self):
        t = make_translator()
        assert await t.translate('', 'ja') == {}
        assert await t.translate('   ', 'ja') == {}
