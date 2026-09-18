"""
语言代码规范化与翻译提示词语言名映射测试
"""
from language_codes import (
    PROMPT_LANGUAGE_NAMES,
    normalize_lang,
    to_prompt_language_name,
    unsupported_pair_message,
)


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


class TestPromptLanguageNames:
    def test_mapped_languages(self):
        assert to_prompt_language_name('zh') == '简体中文'
        assert to_prompt_language_name('zh-cn') == '简体中文'  # 变体先归一
        assert to_prompt_language_name('ja') == '日语'
        assert to_prompt_language_name('en') == '英语'

    def test_unmapped_returns_none(self):
        assert to_prompt_language_name('xx') is None
        assert to_prompt_language_name('') is None

    def test_mapping_is_explicit_lookup_only(self):
        """映射表为显式查表：显示名非空且不等于语言代码本身（回归动态拼接）"""
        for code, name in PROMPT_LANGUAGE_NAMES.items():
            assert isinstance(name, str) and name
            assert name != code

    def test_unsupported_pair_message(self):
        msg = unsupported_pair_message('xx', 'zh')
        assert 'xx' in msg and 'zh' in msg
