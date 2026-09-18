"""翻译模型注册表与 profile 的不变量测试（无网络、无真实模型）"""
import translation_models as tm
from translation_profiles import PROFILES, build_messages, get_profile


class TestRegistry:
    def test_default_exists(self):
        m = tm.default_model()
        assert m.id == tm.DEFAULT_MODEL_ID == 'hy-mt2-1.8b-q4km'
        assert m.profile in PROFILES

    def test_ids_unique(self):
        ids = [m.id for m in tm.REGISTRY]
        assert len(ids) == len(set(ids))

    def test_entries_wellformed(self):
        for m in tm.REGISTRY:
            assert len(m.sha256) == 64 and all(c in '0123456789abcdef' for c in m.sha256)
            assert m.size_bytes > 0
            assert m.filename.endswith('.gguf')
            assert m.revision and len(m.revision) >= 7
            assert m.profile in PROFILES, f'{m.id} 引用了未知 profile'

    def test_get_model(self):
        assert tm.get_model('nope') is None
        assert tm.get_model('hy-mt2-1.8b-q4km') is not None


class TestDownloadSources:
    def test_official_url(self):
        m = tm.default_model()
        urls = tm.resolve_download_sources(m, 'huggingface')
        assert urls == [
            f'https://huggingface.co/{m.repo}/resolve/{m.revision}/{m.filename}'
        ]

    def test_mirror_url(self):
        m = tm.default_model()
        assert 'hf-mirror.com' in tm.resolve_download_sources(m, 'hf-mirror')[0]

    def test_auto_priority(self):
        m = tm.default_model()
        urls = tm.resolve_download_sources(m, 'auto')
        assert urls[0].startswith('https://huggingface.co/')
        assert any('hf-mirror.com' in u for u in urls)

    def test_modelscope_without_repo_is_empty(self):
        m = tm.default_model()
        assert tm.resolve_download_sources(m, 'modelscope') == []

    def test_model_path_layout(self, tmp_path):
        m = tm.default_model()
        assert tm.model_path(m, tmp_path).parent.name == m.id
        assert tm.model_path(m, tmp_path).name == m.filename
        assert tm.is_downloaded(m, tmp_path) is False


class TestProfiles:
    def test_hy_mt2_official_params(self):
        p = get_profile('hy-mt2')
        assert p.sampling['temperature'] == 0.7
        assert p.sampling['top_p'] == 0.6
        assert p.sampling['top_k'] == 20
        assert p.sampling['repetition_penalty'] == 1.05
        assert p.chat_template_kwargs is None

    def test_qwen3_thinking_disabled(self):
        p = get_profile('qwen3')
        assert p.chat_template_kwargs == {'enable_thinking': False}
        assert '--jinja' in p.server_extra_args
        assert p.sampling['presence_penalty'] == 1.5

    def test_build_messages_injects_target_name(self):
        p = get_profile('hy-mt2')
        msgs = build_messages(p, 'こんにちは', '简体中文')
        assert len(msgs) == 1 and msgs[0]['role'] == 'user'
        content = msgs[0]['content']
        assert '简体中文' in content and 'こんにちは' in content
