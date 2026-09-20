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
        tps = None
        if isinstance(item, tuple):
            if len(item) == 3:
                content, finish, tps = item
            else:
                content, finish = item
        else:
            content, finish = item, 'stop'
        body = {
            'choices': [{
                'message': {'content': content},
                'finish_reason': finish,
            }]
        }
        if tps is not None:
            body['timings'] = {'predicted_per_second': tps}
        return httpx.Response(200, json=body)


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
         default='ok', stream=False):
    """构造 Translator + FakeManager + ChatServer

    stream 默认 False：既有整段契约用例保持逐字节旧行为；流式用例显式传
    stream=True / stream=None（None=不注入键，验证声明默认值 True）。
    """
    chat = ChatServer(responses, default=default)
    client = httpx.AsyncClient(transport=httpx.MockTransport(chat.handler))
    mgr = manager if manager is not None else FakeManager()
    translation_cfg = {'device': device, 'target_languages': ['zh']}
    if stream is not None:
        translation_cfg['stream'] = stream
    translation_cfg.update(config or {})
    cfg = {'translation': translation_cfg}
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
# 流式替身（SSE 帧脚本）
# --------------------------------------------------------------------- #

class FakeClock:
    """可控单调时钟：monotonic() 返回当前值，tick() 前进（供节流用例）"""

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def delta_frame(content: str) -> dict:
    """SSE 内容增量帧（delta.content）"""
    return {'choices': [{'delta': {'content': content}, 'finish_reason': None}]}


def role_frame() -> dict:
    """首帧：仅 role、content 为 null（应忽略）"""
    return {'choices': [{'delta': {'role': 'assistant', 'content': None},
                         'finish_reason': None}]}


def final_frame(finish: str = 'stop', tps=None) -> dict:
    """终帧：finish_reason 非空（可带 timings）"""
    frame = {'choices': [{'delta': {}, 'finish_reason': finish}]}
    if tps is not None:
        frame['timings'] = {'predicted_per_second': tps}
    return frame


class StreamChatServer:
    """流式假 llama-server：按脚本产出 SSE 帧。

    脚本元素：dict=数据帧；str=原始行（注释/空行/`[DONE]`/坏 JSON）；
    Exception=在流中抛出（模拟读取期异常）。
    """

    def __init__(self, scripts=None, default=None, clock=None, frame_step=0.0):
        self.scripts = list(scripts or [])
        self.default = (
            default if default is not None
            else [delta_frame('你好'), final_frame('stop', 42.0)]
        )
        self.calls = 0
        self.requests = []
        self.handler_error = None
        self.status_code = 200
        self.clock = clock
        self.frame_step = frame_step

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(json.loads(request.content))
        if self.handler_error is not None:
            err = self.handler_error
            self.handler_error = None
            raise err
        script = self.scripts.pop(0) if self.scripts else self.default
        items = list(script)

        async def gen():
            for item in items:
                if isinstance(item, Exception):
                    raise item
                if self.clock is not None:
                    self.clock.tick(self.frame_step)
                if isinstance(item, str):
                    yield (item + '\n').encode('utf-8')
                else:
                    payload = json.dumps(item, ensure_ascii=False)
                    yield ('data: ' + payload + '\n\n').encode('utf-8')
            yield b'data: [DONE]\n\n'

        return httpx.Response(self.status_code, content=gen())


def make_stream(env, *, config=None, device='cpu', manager=None, scripts=None,
                default=None, clock=None, frame_step=0.0, stream=True):
    """构造流式 Translator + FakeManager + StreamChatServer"""
    server = StreamChatServer(
        scripts, default=default, clock=clock, frame_step=frame_step
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    mgr = manager if manager is not None else FakeManager()
    translation_cfg = {'device': device, 'target_languages': ['zh'], 'stream': stream}
    translation_cfg.update(config or {})
    tr = Translator(
        {'translation': translation_cfg}, manager=mgr, client=client,
        cache_root=env.tmp_path,
    )
    return tr, mgr, server


def make_stream_ready(env, *, resolved='cpu', device='cpu', **kwargs):
    """已初始化且 server 就绪的流式 Translator"""
    tr, mgr, server = make_stream(env, device=device, **kwargs)
    tr._initialized = True
    tr._resolved_device = resolved
    tr._device_reason = 'user' if resolved == 'cpu' else 'auto'
    mgr.is_running = True
    mgr.is_ready = True
    mgr.model_path = model_path(tr._model, tr.cache_root)
    return tr, mgr, server


def collect_partials():
    """返回 (list, callback)：callback 记录 (目标语言, 累积译文, 首帧标志)"""
    seen = []

    async def on_partial(tgt: str, text: str, is_first: bool) -> None:
        seen.append((tgt, text, is_first))

    return seen, on_partial


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

    async def test_success_carries_tps_metric(self, env):
        """translate_with_metrics：timings.predicted_per_second → tps 指标"""
        tr, mgr, chat = make_ready(env, responses=[('你好', 'stop', 46.47)])
        results, metrics = await tr.translate_with_metrics('こんにちは', 'ja')
        assert results == {'zh': '你好'}
        assert metrics == {'zh': pytest.approx(46.47)}

    async def test_missing_timings_no_metric(self, env):
        """响应缺失 timings → 无指标，翻译结果不受影响"""
        tr, mgr, chat = make_ready(env, responses=['你好'])
        results, metrics = await tr.translate_with_metrics('こんにちは', 'ja')
        assert results == {'zh': '你好'}
        assert metrics == {}

    @pytest.mark.parametrize('bad', ['fast', 0, -1.5, True])
    async def test_invalid_timings_no_metric(self, env, bad):
        """timings 字段非数值/非正数 → 一律不产生指标"""
        tr, mgr, chat = make_ready(env, responses=[('你好', 'stop', bad)])
        results, metrics = await tr.translate_with_metrics('こんにちは', 'ja')
        assert results == {'zh': '你好'}
        assert metrics == {}

    async def test_unsupported_pair_has_no_metric(self, env):
        """未支持语言对（占位串）不产生指标"""
        tr, mgr, chat = make_ready(env, config={'target_languages': ['xx']})
        results, metrics = await tr.translate_with_metrics('text', 'ja')
        assert '未支持的语言对' in results['xx']
        assert metrics == {}

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

    async def test_retry_metric_uses_successful_attempt(self, env):
        """退化重试：指标取最终成功那一次生成的 tps，而非被丢弃的首次"""
        tr, mgr, chat = make_ready(
            env, responses=[('你好' * 30, 'stop', 11.0), ('你好', 'stop', 42.0)]
        )
        results, metrics = await tr.translate_with_metrics('こんにちは', 'ja')
        assert results == {'zh': '你好'}
        assert metrics == {'zh': pytest.approx(42.0)}

    async def test_persistent_degenerate_has_no_metric(self, env):
        """两次均退化 → 占位串且无指标"""
        tr, mgr, chat = make_ready(
            env, responses=[('你好' * 30, 'stop', 11.0), ('你好' * 30, 'stop', 12.0)]
        )
        results, metrics = await tr.translate_with_metrics('こんにちは', 'ja')
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert metrics == {}

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

    async def test_failed_request_has_no_metric(self, env):
        """请求失败 → 占位串且不产生指标"""
        tr, mgr, chat = make_ready(
            env, responses=[httpx.ReadTimeout('simulated timeout')]
        )
        results, metrics = await tr.translate_with_metrics('こんにちは', 'ja')
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert metrics == {}

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

    async def test_warmup_stream_enabled_keeps_gpu(self, env):
        """回归（add-llm-streaming-output 真机实测发现）：stream=True 时热身
        必须走 SSE 链路；若误用非流式解析会失败并把 GPU 误判为 load_failed
        而静默降级 CPU。"""
        env.cuda = True
        tr, mgr, server = make_stream(
            env, device='cuda',
            default=[role_frame(), delta_frame('ok'), final_frame('stop', 42.0)],
        )
        await tr.initialize()
        await tr.ensure_default()

        assert [s.n_gpu_layers for s in mgr.starts] == [99]
        assert tr.resolved_device == 'cuda'
        assert server.requests[0]['stream'] is True

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


# --------------------------------------------------------------------- #
# 流式：SSE 解析
# --------------------------------------------------------------------- #

class TestStreamSseParsing:
    async def test_parses_frames_skipping_noise_and_bad_json(self, env):
        """保活注释/空行/坏 JSON 帧全部跳过，内容累积、终帧 finish/timings 生效"""
        script = [
            role_frame(),          # 首帧 role/null content 忽略
            ': keep-alive',        # 保活注释行
            '',                    # 空行
            delta_frame('你好'),
            'data: {bad json',     # 单帧解析失败不中断
            'data: 123',           # 合法 JSON 但非对象，同样跳过
            delta_frame('世界'),
            final_frame('stop', 55.5),
        ]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        deltas = []

        async def on_delta(accumulated):
            deltas.append(accumulated)

        content, finish, tps = await tr._request_chat_stream(
            'こんにちは', '简体中文', on_delta=on_delta
        )
        assert content == '你好世界'
        assert finish == 'stop'
        assert tps == pytest.approx(55.5)
        assert deltas == ['你好', '你好世界']
        assert server.requests[0]['stream'] is True

    async def test_done_terminates_before_later_frames(self, env):
        script = [delta_frame('你好'), 'data: [DONE]', delta_frame('后面')]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        content, finish, tps = await tr._request_chat_stream('x', '简体中文')
        assert content == '你好'
        assert finish is None
        assert tps is None

    async def test_missing_timings_returns_none(self, env):
        script = [delta_frame('你好'), final_frame('stop')]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        content, finish, tps = await tr._request_chat_stream('x', '简体中文')
        assert content == '你好'
        assert finish == 'stop'
        assert tps is None


# --------------------------------------------------------------------- #
# 流式：持有缓冲（hold-back）
# --------------------------------------------------------------------- #

class TestStreamHoldBack:
    async def test_fence_not_pushed_until_release(self, env):
        """markdown 围栏未闭合期间不推送；遇终止标点释放后首帧即净化内容"""
        script = [
            delta_frame('```'),
            delta_frame('\n你'),
            delta_frame('好'),
            delta_frame('世界'),
            delta_frame('！'),
            final_frame('stop'),
        ]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        seen, on_partial = collect_partials()
        results, _ = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': '你好世界！'}
        assert [t for _, t, _ in seen] == ['你好世界！']
        assert seen[0][2] is True  # attempt 0 首次推送 = 首帧

    async def test_prefix_not_pushed_until_release(self, env):
        """'Translation:' 前缀期间不推送（净化后仍不足阈值）；释放后推送净化文本"""
        script = [
            delta_frame('Transl'),
            delta_frame('ation:'),
            delta_frame('你好'),
            delta_frame('世界'),
            delta_frame('你好'),
            delta_frame('世界'),
            final_frame('stop'),
        ]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        seen, on_partial = collect_partials()
        results, _ = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': '你好世界你好世界'}
        assert [t for _, t, _ in seen] == ['你好世界你好世界']
        assert all('Translation' not in t for _, t, _ in seen)


# --------------------------------------------------------------------- #
# 流式：节流合帧
# --------------------------------------------------------------------- #

LONG_CN = '你好世界一二三四五六七八九十甲乙丙丁戊己庚辛'  # 22 个互异字符


class TestStreamThrottle:
    async def test_char_based_coalescing(self, env, monkeypatch):
        """时间未到但按字符增量合帧：推送次数显著少于增量数"""
        monkeypatch.setattr(tr_mod, '_STREAM_MIN_INTERVAL_MS', 10_000_000)
        clock = FakeClock()
        monkeypatch.setattr(tr_mod, 'time', clock)
        script = [delta_frame(ch) for ch in LONG_CN] + [final_frame('stop')]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        seen, on_partial = collect_partials()
        results, _ = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': LONG_CN}
        assert 0 < len(seen) < len(LONG_CN)
        assert [t for _, t, _ in seen][-1] == LONG_CN

    async def test_time_based_immediate_push(self, env, monkeypatch):
        """每帧间隔 ≥ 节流窗口：逐增量即时推送"""
        monkeypatch.setattr(tr_mod, '_STREAM_MIN_INTERVAL_MS', 50)
        monkeypatch.setattr(tr_mod, '_STREAM_MIN_CHARS', 10 ** 9)
        clock = FakeClock()
        monkeypatch.setattr(tr_mod, 'time', clock)
        script = [delta_frame(ch) for ch in LONG_CN] + [final_frame('stop')]
        tr, mgr, server = make_stream_ready(
            env, scripts=[script], clock=clock, frame_step=0.1
        )
        seen, on_partial = collect_partials()
        results, _ = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': LONG_CN}
        # 释放点（第 8 字符）起每个增量各推一帧
        assert len(seen) == len(LONG_CN) - 7
        assert [t for _, t, _ in seen][-1] == LONG_CN

    async def test_final_partial_forced_before_finalize(self, env):
        """留有余量：末帧强制推送完整净化文本（即使未达节流窗口）"""
        script = [
            delta_frame('你好世界'),   # len 4 < 8，不释放
            final_frame('stop'),
        ]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        seen, on_partial = collect_partials()
        results, _ = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': '你好世界'}
        # 未达持有阈值且无终止标点 → 释放失败，不推送
        assert seen == []


# --------------------------------------------------------------------- #
# 流式：复读前移止损与可见重写
# --------------------------------------------------------------------- #

class TestStreamRewrite:
    async def test_repeat_loop_abort_then_visible_rewrite(self, env):
        attempt0 = [
            delta_frame('你好' * 10),
            delta_frame('你好' * 10),  # 累积触发复读 → 立即中止
            delta_frame('你好' * 10),
            final_frame('stop', 11.0),
        ]
        attempt1 = [
            delta_frame('你好世界。'),
            final_frame('stop', 42.0),
        ]
        tr, mgr, server = make_stream_ready(env, scripts=[attempt0, attempt1])
        seen, on_partial = collect_partials()
        results, metrics = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': '你好世界。'}
        assert metrics == {'zh': pytest.approx(42.0)}
        assert server.calls == 2
        assert server.requests[0]['repetition_penalty'] == pytest.approx(1.05)
        assert server.requests[1]['repetition_penalty'] == pytest.approx(1.3)
        texts = [t for _, t, _ in seen]
        assert texts[0].startswith('你好')      # 第一遍已推送内容
        assert texts[-1] == '你好世界。'          # 第二遍替换 = 可见重写
        assert texts[-1] != texts[0]
        assert tr._consecutive_failures == 0

    async def test_repeat_loop_both_attempts_placeholder(self, env):
        script = [
            delta_frame('你好' * 10),
            delta_frame('你好' * 10),
            final_frame('stop', 9.0),
        ]
        tr, mgr, server = make_stream_ready(env, scripts=[script, script])
        seen, on_partial = collect_partials()
        results, metrics = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert metrics == {}
        assert server.calls == 2
        assert tr._consecutive_failures == 0  # 退化不算引擎健康失败


# --------------------------------------------------------------------- #
# 流式：关流回退与默认值
# --------------------------------------------------------------------- #

class TestStreamFallback:
    async def test_disabled_uses_blocking_path_no_partials(self, env):
        tr, mgr, chat = make_ready(env, stream=False, responses=['你好'])
        seen, on_partial = collect_partials()
        results, metrics = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': '你好'}
        assert metrics == {}
        assert seen == []
        assert chat.requests[0]['stream'] is False
        assert tr._stream_enabled is False

    async def test_default_true_when_key_absent(self, env):
        tr, mgr, chat = make(env, stream=None)
        assert tr._stream_enabled is True

    async def test_build_payload_stream_flag(self, env):
        tr_on, _, _ = make(env, stream=True)
        tr_off, _, _ = make(env, stream=False)
        assert tr_on._build_payload('x', '简体中文', None, 64)['stream'] is True
        assert tr_off._build_payload('x', '简体中文', None, 64)['stream'] is False


# --------------------------------------------------------------------- #
# 流式：异常 / 超时路径
# --------------------------------------------------------------------- #

class TestStreamFailure:
    async def test_request_error_placeholder(self, env):
        tr, mgr, server = make_stream_ready(env)
        server.handler_error = httpx.ReadTimeout('simulated timeout')
        seen, on_partial = collect_partials()
        results, metrics = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert metrics == {}
        assert seen == []
        assert tr._consecutive_failures == 1

    async def test_http_error_placeholder(self, env):
        tr, mgr, server = make_stream_ready(env)
        server.status_code = 500
        seen, on_partial = collect_partials()
        results, metrics = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert metrics == {}
        assert seen == []
        assert tr._consecutive_failures == 1

    async def test_midstream_timeout_placeholder_after_partials(self, env):
        script = [delta_frame('你好世界！'), httpx.ReadTimeout('mid-stream timeout')]
        tr, mgr, server = make_stream_ready(env, scripts=[script])
        seen, on_partial = collect_partials()
        results, metrics = await tr.translate_with_metrics(
            'こんにちは', 'ja', on_partial=on_partial
        )
        assert results == {'zh': FAILURE_PLACEHOLDER}
        assert metrics == {}
        assert [t for _, t, _ in seen] == ['你好世界！']  # 已推部分帧
        assert tr._consecutive_failures == 1
