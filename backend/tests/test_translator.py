"""
翻译引擎（llama-server sidecar 客户端）单元测试

替身策略：FakeManager 模拟进程管理器（start/stop/健康状态），
httpx.MockTransport 模拟 llama-server 的 /v1/chat/completions；
探针/二进制/下载判定经 monkeypatch 注入，不触碰真实进程、网络与文件系统。
"""
import asyncio
import json

import httpx
import pytest

import translator as tr_mod
from translator import (
    FAILURE_PLACEHOLDER,
    Translator,
)
from translation_models import REGISTRY, model_path

DEFAULT_ID = 'hy-mt2-1.8b-q4km'
QWEN_ID = 'qwen3-1.7b-q4km'


# --------------------------------------------------------------------- #
# 替身
# --------------------------------------------------------------------- #

class FakeManager:
    """假 llama-server 管理器：记录 start 配置、按计数注入失败"""

    def __init__(self, *, fail_starts: int = 0):
        self.starts = []
        self.stop_calls = 0
        self.is_running = False
        self.is_ready = False
        self.port = 18080
        self.model_path = None
        self.fail_starts = fail_starts

    async def start(self, config):
        self.starts.append(config)
        if self.fail_starts > 0:
            self.fail_starts -= 1
            self.is_running = False
            self.is_ready = False
            raise RuntimeError('fake spawn failed')
        self.model_path = config.model_path
        self.is_running = True
        self.is_ready = True

    async def stop(self):
        self.stop_calls += 1
        self.is_running = False
        self.is_ready = False
        self.model_path = None


class ChatServer:
    """按序应答的假 chat 补全服务；队列空时返回默认回答"""

    def __init__(self, responses=None, default: str = 'ok'):
        self.responses = list(responses or [])
        self.default = default
        self.calls = 0
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(json.loads(request.content))
        item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, Exception):
            raise item
        if isinstance(item, tuple):
            content, finish = item
        else:
            content, finish = item, 'stop'
        return httpx.Response(200, json={
            'choices': [{
                'message': {'content': content},
                'finish_reason': finish,
            }]
        })


class Env:
    """测试替身环境（探针/二进制/下载状态）"""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.cuda = False
        self.downloaded = True
        self.downloads = []
        self.exe = tmp_path / 'llama-server.exe'
        self.exe.write_bytes(b'stub')


@pytest.fixture
def env(monkeypatch, tmp_path):
    e = Env(tmp_path)
    monkeypatch.setattr(
        tr_mod, 'probe_compute',
        lambda: {'translation': {
            'cuda_available': e.cuda, 'source': 'none', 'detail': 'test',
        }},
    )
    monkeypatch.setattr(tr_mod, 'resolve_device_binary', lambda root, device: e.exe)
    monkeypatch.setattr(
        tr_mod, 'is_downloaded', lambda model, cache_root=None: e.downloaded
    )

    async def fake_download(model, *, source, progress, cache_root=None):
        e.downloads.append(model.id)
        return model_path(model, cache_root)

    monkeypatch.setattr(tr_mod, 'download_model', fake_download)
    return e


def make(env, *, config=None, device='cpu', manager=None, responses=None,
         default='ok'):
    """构造 Translator + FakeManager + ChatServer"""
    chat = ChatServer(responses, default=default)
    client = httpx.AsyncClient(transport=httpx.MockTransport(chat.handler))
    mgr = manager if manager is not None else FakeManager()
    cfg = {
        'translation': {
            'device': device,
            'target_languages': ['zh'],
            **(config or {}),
        }
    }
    tr = Translator(cfg, manager=mgr, client=client, cache_root=env.tmp_path)
    return tr, mgr, chat


def make_ready(env, *, resolved='cpu', device='cpu', **kwargs):
    """已初始化且 server 就绪的 Translator（跳过 warmup 请求）"""
    tr, mgr, chat = make(env, device=device, **kwargs)
    tr._initialized = True
    tr._resolved_device = resolved
    tr._device_reason = 'user' if resolved == 'cpu' else 'auto'
    mgr.is_running = True
    mgr.is_ready = True
    mgr.model_path = model_path(tr._model, tr.cache_root)
    return tr, mgr, chat


# --------------------------------------------------------------------- #
# 翻译路由与请求构造
# --------------------------------------------------------------------- #

class TestTranslateRouting:
    async def test_success_payload_and_result(self, env):
        tr, mgr, chat = make_ready(env, responses=['你好'])
        results = await tr.translate('こんにちは', 'ja')
        assert results == {'zh': '你好'}

        req = chat.requests[0]
        assert req['stream'] is False
        assert req['temperature'] == 0.7
        assert req['max_tokens'] >= 64
        assert req['messages'][0]['content'].startswith('将以下文本翻译为简体中文')
        assert 'こんにちは' in req['messages'][0]['content']
        assert 'chat_template_kwargs' not in req

    async def test_same_language_skipped(self, env):
        tr, mgr, chat = make_ready(env)
        assert await tr.translate('你好世界', 'zh-cn') == {}
        assert chat.calls == 0

    async def test_unsupported_target_placeholder(self, env):
        tr, mgr, chat = make_ready(env, config={'target_languages': ['xx']})
        results = await tr.translate('text', 'ja')
        assert '未支持的语言对' in results['xx']
        assert chat.calls == 0

    async def test_missing_source_empty_and_uninitialized(self, env):
        tr, mgr, chat = make_ready(env)
        assert await tr.translate('text', None) == {}
        assert await tr.translate('', 'ja') == {}
        assert await tr.translate('   ', 'ja') == {}

        tr._initialized = False
        assert await tr.translate('text', 'ja') == {}
        assert chat.calls == 0

    async def test_not_ready_skips_without_request(self, env):
        tr, mgr, chat = make_ready(env)
        mgr.is_ready = False
        assert await tr.translate('こんにちは', 'ja') == {}
        assert chat.calls == 0
        assert tr.is_ready is False

    async def test_qwen3_profile_disables_thinking(self, env):
        tr, mgr, chat = make_ready(
            env,
            config={'default_model': QWEN_ID, 'target_languages': ['en']},
            responses=['hello'],
        )
        results = await tr.translate('你好', 'ja')
        assert results == {'en': 'hello'}
        req = chat.requests[0]
        assert req['chat_template_kwargs'] == {'enable_thinking': False}
        assert req['presence_penalty'] == 1.5


# --------------------------------------------------------------------- #
# 输出净化
# --------------------------------------------------------------------- #

class TestPurify:
    def test_strip_fence(self):
        assert Translator._purify('```\n你好\n```') == '你好'
        assert Translator._purify('```zh\n你好\n```') == '你好'

    def test_strip_quotes(self):
        assert Translator._purify('"你好"') == '你好'
        assert Translator._purify('“你好”') == '你好'

    def test_strip_prefix(self):
        assert Translator._purify('Translation: hello') == 'hello'
        assert Translator._purify('翻译：你好') == '你好'

    def test_combined_and_trim(self):
        raw = '```\nTranslation: "你好"\n```'
        assert Translator._purify(raw) == '你好'


# --------------------------------------------------------------------- #
# 退化检测与重试
# --------------------------------------------------------------------- #

class TestDegenerateRetry:
    async def test_repeat_loop_retried_with_boosted_penalty(self, env):
        tr, mgr, chat = make_ready(
            env, responses=[('你好' * 30, 'stop'), '你好']
        )
        results = await tr.translate('こんにちは', 'ja')
        assert results == {'zh': '你好'}
        assert chat.calls == 2
        first, second = chat.requests
        assert first['repetition_penalty'] == pytest.approx(1.05)
        assert second['repetition_penalty'] == pytest.approx(1.3)

    async def test_max_tokens_hit_is_degenerate(self, env):
        tr, mgr, chat = make_ready(
            env, responses=[('你好' * 30, 'length'), '你好']
        )
        results = await tr.translate('こんにちは', 'ja')
        assert results == {'zh': '你好'}
        assert chat.calls == 2

    async def test_echo_is_degenerate(self, env):
        tr, mgr, chat = make_ready(
            env, responses=[('将以下文本翻译为简体中文，注意只需要输出翻译后的结果，不要额外解释：' * 2, 'stop'), '你好']
        )
        results = await tr.translate('こんにちは', 'ja')
        assert results == {'zh': '你好'}
        assert chat.calls == 2

    async def test_persistent_degenerate_returns_placeholder(self, env):
        tr, mgr, chat = make_ready(env, responses=['你好' * 30, '你好' * 30])
        results = await tr.translate('こんにちは', 'ja')
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert chat.calls == 2
        # 退化不算引擎健康失败，不触发降级
        assert tr._consecutive_failures == 0


# --------------------------------------------------------------------- #
# 请求超时 / 运行期失败 / 降级
# --------------------------------------------------------------------- #

class TestRuntimeHealth:
    async def test_timeout_returns_placeholder_and_counts(self, env):
        tr, mgr, chat = make_ready(
            env, responses=[httpx.ReadTimeout('simulated timeout')]
        )
        results = await tr.translate('こんにちは', 'ja')
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert tr._consecutive_failures == 1

    async def test_persistent_failure_event_on_cpu(self, env):
        health = []
        tr, mgr, chat = make_ready(env, default='ok')
        tr.set_health_callback(health.append)
        chat.default = httpx.ConnectError('down')

        for _ in range(3):
            await tr.translate('こんにちは', 'ja')

        assert [h['event'] for h in health] == ['persistent_failure']
        assert health[0]['failures'] == 3

        await tr.translate('こんにちは', 'ja')  # 不重复广播
        assert len(health) == 1

    async def test_cuda_failure_degrades_to_cpu_once(self, env):
        health = []
        tr, mgr, chat = make_ready(env, resolved='cuda', device='cuda')
        tr.set_health_callback(health.append)
        chat.responses = [httpx.ReadTimeout('boom')]  # 本次请求失败

        await tr.translate('こんにちは', 'ja')

        for _ in range(100):
            if tr.resolved_device == 'cpu' and tr.device_reason == 'runtime_failed':
                break
            await asyncio.sleep(0.01)

        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'runtime_failed'
        assert mgr.stop_calls >= 1
        assert mgr.starts[-1].n_gpu_layers == 0  # CPU 构建
        assert [h['event'] for h in health] == ['runtime_degraded']
        assert health[0]['reason'] == 'runtime_failed'

        # 一次性：再失败不再重复降级
        await tr._degrade_runtime_to_cpu()
        assert len(health) == 1


# --------------------------------------------------------------------- #
# ensure_default / 下载 / GPU 失败静默降级
# --------------------------------------------------------------------- #

class TestEnsureDefault:
    async def test_initialize_resolves_device_without_spawn(self, env):
        tr, mgr, chat = make(env, device='auto')
        await tr.initialize()
        assert tr._initialized is True
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'no_cuda'
        assert mgr.starts == []  # 拉起由后台预载负责

    async def test_downloads_missing_model_then_starts(self, env):
        env.downloaded = False
        tr, mgr, chat = make(env)
        await tr.initialize()
        await tr.ensure_default()

        assert env.downloads == [DEFAULT_ID]
        assert len(mgr.starts) == 1
        assert mgr.starts[0].n_gpu_layers == 0
        assert tr.is_ready is True

    async def test_gpu_start_failure_falls_back_to_cpu(self, env):
        env.cuda = True
        tr, mgr, chat = make(env, device='cuda', manager=FakeManager(fail_starts=1))
        await tr.initialize()
        await tr.ensure_default()

        assert len(mgr.starts) == 2
        assert mgr.starts[0].n_gpu_layers == 99
        assert mgr.starts[1].n_gpu_layers == 0
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'load_failed'

    async def test_warmup_failure_falls_back_to_cpu(self, env):
        env.cuda = True
        tr, mgr, chat = make(
            env, device='cuda',
            responses=[httpx.ConnectError('warmup refused'), 'ok'],
        )
        await tr.initialize()
        await tr.ensure_default()

        assert [s.n_gpu_layers for s in mgr.starts] == [99, 0]
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'load_failed'

    async def test_new_default_model_key_shapes_cpu(self, env):
        tr, mgr, chat = make(env, config={'n_ctx': 2048})
        await tr.initialize()
        await tr.ensure_default()
        cfg = mgr.starts[0]
        assert cfg.n_ctx == 2048


# --------------------------------------------------------------------- #
# change_llm
# --------------------------------------------------------------------- #

class TestChangeLlm:
    async def test_invalid_model_raises(self, env):
        tr, mgr, chat = make_ready(env)
        with pytest.raises(ValueError):
            await tr.change_llm('nope')
        with pytest.raises(ValueError):
            await tr.change_llm(None)
        assert mgr.stop_calls == 0

    async def test_switch_terminates_old_and_starts_new(self, env):
        tr, mgr, chat = make_ready(env)
        await tr.change_llm(QWEN_ID)

        assert tr._model.id == QWEN_ID
        assert tr._profile.key == 'qwen3'
        assert mgr.stop_calls == 1
        assert len(mgr.starts) == 1
        assert mgr.starts[0].extra_args == ('--jinja',)  # qwen3 服务端参数
        assert tr.is_ready is True
        assert tr._switching is False

    async def test_same_model_ready_is_idempotent(self, env):
        tr, mgr, chat = make_ready(env)
        await tr.change_llm(tr._model.id)
        assert mgr.stop_calls == 0
        assert mgr.starts == []

    async def test_same_model_not_ready_retries_without_stop(self, env):
        tr, mgr, chat = make_ready(env)
        mgr.is_ready = False
        await tr.change_llm(tr._model.id)
        assert mgr.stop_calls == 0
        assert len(mgr.starts) == 1
        assert tr.is_ready is True

    async def test_switching_flag_gates_readiness(self, env):
        tr, mgr, chat = make_ready(env)
        assert tr.is_ready is True
        tr._switching = True
        assert tr.is_ready is False


# --------------------------------------------------------------------- #
# change_device
# --------------------------------------------------------------------- #

class TestChangeDevice:
    async def test_invalid_device_raises(self, env):
        tr, mgr, chat = make_ready(env)
        with pytest.raises(ValueError):
            await tr.change_device('tpu')

    async def test_same_device_is_idempotent(self, env):
        tr, mgr, chat = make_ready(env, device='cpu')
        await tr.change_device('cpu')
        assert mgr.stop_calls == 0

    async def test_switch_resets_resolution_and_restarts(self, env):
        tr, mgr, chat = make_ready(env, device='cpu', resolved='cpu')
        await tr.change_device('cuda')  # 探针无 CUDA → no_cuda 降级
        assert tr.device == 'cuda'
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'no_cuda'
        assert mgr.stop_calls == 1
        assert mgr.starts[-1].n_gpu_layers == 0

    async def test_switch_when_never_loaded_only_resolves(self, env):
        tr, mgr, chat = make(env)
        await tr.initialize()
        await tr.change_device('cuda')
        assert tr.resolved_device == 'cpu'
        assert tr.device_reason == 'no_cuda'
        assert mgr.starts == []  # 未加载过 → 不后台重拉


# --------------------------------------------------------------------- #
# 配置与信息
# --------------------------------------------------------------------- #

class TestConfigAndInfo:
    async def test_get_model_info_fields(self, env):
        tr, mgr, chat = make_ready(env)
        info = tr.get_model_info()
        assert info['model'] == DEFAULT_ID
        assert 'primary_model' not in info
        assert 'fallback_model' not in info
        assert 'nllb_loaded' not in info
        assert 'nllb_languages' not in info
        assert info['device'] == 'cpu'
        assert info['resolved_device'] == 'cpu'
        assert 'device_reason' in info

        ids = [m['id'] for m in info['available_models']]
        assert ids == [m.id for m in REGISTRY]
        current = [m for m in info['available_models'] if m['current']]
        assert [m['id'] for m in current] == [DEFAULT_ID]
        assert all('downloaded' in m and 'size_bytes' in m for m in info['available_models'])

    async def test_legacy_config_keys_ignored(self, env):
        tr, mgr, chat = make(
            env,
            config={
                'primary_model': 'Helsinki-NLP/opus-mt-ja-zh',
                'fallback_model': 'facebook/nllb-200-distilled-600M',
                'lazy_load': True,
                'preload_primary': True,
            },
        )
        assert tr._model.id == DEFAULT_ID

    async def test_unknown_default_model_falls_back(self, env):
        tr, mgr, chat = make(env, config={'default_model': 'ghost-model'})
        assert tr._model.id == DEFAULT_ID

    async def test_registry_augmentation_from_config(self, env):
        extra = {
            'id': 'my-model',
            'display_name': '自定义模型',
            'repo': 'someone/my-model-GGUF',
            'revision': 'abc123',
            'filename': 'my-model.gguf',
            'size_bytes': 1024,
            'sha256': 'a' * 64,
        }
        tr, mgr, chat = make(env, config={'models': [extra, {'id': 'broken'}]})
        ids = [m.id for m in tr._registry.values()]
        assert 'my-model' in ids
        assert 'broken' not in ids

        # 可切换到增补模型（文件按 is_downloaded 替身为就绪）
        tr._initialized = True
        mgr.is_ready = True
        mgr.model_path = model_path(tr._model, tr.cache_root)
        await tr.change_llm('my-model')
        assert tr._model.id == 'my-model'

    async def test_download_progress_reported(self, env):
        env.downloaded = False
        progress = []
        tr, mgr, chat = make(env)
        tr.set_download_progress_callback(
            lambda name, pct, msg: progress.append((name, pct, msg))
        )
        await tr.initialize()
        await tr.ensure_default()
        assert any('未下载' in msg for _, _, msg in progress)

    async def test_stop_closes_manager_and_client(self, env):
        tr, mgr, chat = make_ready(env)
        await tr.stop()
        assert mgr.stop_calls == 1
        assert tr._client is None
