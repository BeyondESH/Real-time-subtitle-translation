"""
ASREngine（sherpa-onnx Fun-ASR-Nano）运行期健壮性与语义测试（假识别器/假探针，无 GPU/网络）

覆盖 replace-asr-engine-with-funasr-nano：
- 热身失败 → CPU 重建且 reason=runtime_failed（加载成功 ≠ 可推理）
- 运行期异常 → 一次性降级（防抖，不重复重载）
- 推理超时 → 执行器被替换且后续调用不受阻（隔离卡死线程）
- 并发 initialize/change_model 串行化（无后写覆盖）
- 单引擎模型语义：change_model 同值幂等、其他值 ValueError（invalid_model）
- 源语言：默认 ja、非法回退、change_source_language 校验与热切换调度
- notify_stall 仅在 cuda 时触发降级；CPU 连续失败仅广播一次
- 解码参数：itn/hotwords/num_threads 默认、覆盖、非法回退告警
- 随包 CUDA 运行库 PATH 注入
"""
import asyncio
import logging
import os
import threading
import time
import types

import numpy as np
import pytest

import asr_engine as asr_mod
from asr_engine import ASREngine


def _probe(cuda_available):
    return {
        'cuda_available': bool(cuda_available),
        'source': 'onnxruntime',
        'detail': f'test probe cuda={cuda_available}',
    }


class FakeStream:
    """可编程假解码流：behavior=ok/raise/hang"""

    def __init__(self, behavior):
        self.behavior = behavior
        self._text = 'hi'

    def accept_waveform(self, sample_rate, samples):
        pass

    @property
    def result(self):
        return types.SimpleNamespace(text=self._text)


class FakeRecognizer:
    """可编程假识别器：behavior=ok/raise/hang"""

    def __init__(self, provider, behavior='ok'):
        self.provider = provider
        self.behavior = behavior
        self.calls = 0

    def create_stream(self):
        return FakeStream(self.behavior)

    def decode_stream(self, stream):
        self.calls += 1
        if self.behavior == 'raise':
            raise RuntimeError(
                'CUDA provider unavailable: cudnn64_9.dll is not found'
            )
        if self.behavior == 'hang':
            time.sleep(1.0)  # 模拟卡死调用（由超时隔离；保持有界便于测试退出）
        stream._text = 'hi'


def make_engine(monkeypatch, cuda_available, behavior='ok', device='auto',
                config=None):
    """构造引擎：monkeypatch 探针/模型文件检查与识别器构造"""
    created = []

    def fake_builder(provider):
        created.append(provider)
        if provider == 'cuda' and behavior == 'cuda_raise':
            return FakeRecognizer(provider, behavior='raise')
        return FakeRecognizer(provider, behavior=behavior)

    monkeypatch.setattr(
        asr_mod, 'probe_compute', lambda: {'asr': _probe(cuda_available)}
    )
    # 模型文件视为已就绪（下载链路由 test_asr_models 单独覆盖）
    monkeypatch.setattr(asr_mod, 'is_asr_model_downloaded', lambda: True)
    engine = ASREngine({'asr': {'device': device, **(config or {})}})
    monkeypatch.setattr(engine, '_build_recognizer', fake_builder)
    return engine, created


def audio(seconds=1.0):
    return np.zeros(int(16000 * seconds), dtype=np.float32)


async def wait_for(cond, timeout=2.0):
    """轮询等待条件成立（bg 任务/线程完成）"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


class TestWarmUpValidation:
    async def test_warm_up_failure_degrades_to_cpu_runtime_failed(self, monkeypatch):
        """加载成功但推理不可用（缺 cuDNN）→ CPU 重建 + runtime_failed"""
        engine, created = make_engine(
            monkeypatch, True, behavior='cuda_raise'
        )
        await engine.initialize()  # MUST NOT raise

        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'runtime_failed'
        assert created == ['cuda', 'cpu']

    async def test_warm_up_failure_on_cpu_bubbles(self, monkeypatch):
        """CPU 端到端验证同样失败 → 初始化失败语义（冒泡）"""
        engine, _ = make_engine(monkeypatch, False, behavior='raise')
        with pytest.raises(RuntimeError):
            await engine.initialize()

    async def test_warm_up_success_keeps_gpu(self, monkeypatch):
        """热身通过：GPU 路径保持（回归）"""
        engine, created = make_engine(monkeypatch, True)
        await engine.initialize()

        assert engine.resolved_device == 'cuda'
        assert engine.device_reason == 'auto'
        assert created == ['cuda']


class TestRuntimeDegrade:
    async def test_runtime_failure_degrades_once(self, monkeypatch):
        """运行期异常 → 一次性降级；再次失败/停滞通知不重复重载"""
        recognizer = {}
        created = []
        events = []

        def fake_builder(provider):
            created.append(provider)
            r = FakeRecognizer(provider)
            recognizer[provider] = r
            return r

        engine, _ = make_engine(monkeypatch, True)
        monkeypatch.setattr(engine, '_build_recognizer', fake_builder)
        engine.set_health_callback(events.append)
        await engine.initialize()
        assert engine.resolved_device == 'cuda'

        # 运行期推理失败 → 调度降级
        recognizer['cuda'].behavior = 'raise'
        assert await engine.transcribe(audio()) is None
        assert await wait_for(lambda: engine.resolved_device == 'cpu')

        assert created == ['cuda', 'cpu']
        assert engine.device_reason == 'runtime_failed'
        assert [e['event'] for e in events] == ['runtime_degraded']

        # 防抖：再次通知停滞不产生第二次重载
        engine.notify_stall()
        await asyncio.sleep(0.05)
        assert created == ['cuda', 'cpu']

    async def test_notify_stall_only_degrades_when_cuda(self, monkeypatch):
        """cpu 上 notify_stall 不做任何事"""
        engine, created = make_engine(monkeypatch, False)
        await engine.initialize()
        assert engine.resolved_device == 'cpu'

        engine.notify_stall()
        await asyncio.sleep(0.05)
        assert created == ['cpu']

    async def test_cpu_persistent_failure_notified_once(self, monkeypatch):
        """CPU 连续失败 ≥3 次仅广播一次（不刷屏、不无界重试）"""
        events = []

        engine, _ = make_engine(monkeypatch, False, behavior='raise')
        engine.set_health_callback(events.append)
        # 直接进入初始化会因热身失败冒泡 → 手工模拟已初始化状态
        engine._initialized = True
        engine._model = FakeRecognizer('cpu', behavior='raise')
        engine._resolved_device = 'cpu'
        engine._device_reason = 'user'

        for _ in range(4):
            assert await engine.transcribe(audio()) is None

        assert [e['event'] for e in events] == ['persistent_failure']
        assert events[0]['failures'] == 3

    async def test_success_resets_failure_streak(self, monkeypatch):
        """成功一次即清零连续失败计数"""
        events = []

        engine, _ = make_engine(monkeypatch, False)
        engine.set_health_callback(events.append)
        engine._initialized = True
        model = FakeRecognizer('cpu')
        engine._model = model
        engine._resolved_device = 'cpu'
        engine._device_reason = 'user'

        model.behavior = 'raise'
        assert await engine.transcribe(audio()) is None
        assert await engine.transcribe(audio()) is None
        model.behavior = 'ok'
        assert await engine.transcribe(audio()) is not None  # 成功 → 清零
        model.behavior = 'raise'
        assert await engine.transcribe(audio()) is None
        assert await engine.transcribe(audio()) is None
        assert await engine.transcribe(audio()) is None  # 重新数到第 3 次

        assert [e['event'] for e in events] == ['persistent_failure']


class TestTimeoutIsolation:
    async def test_timeout_resets_executor_and_next_call_works(self, monkeypatch):
        """超时 → 执行器被弃用替换；后续调用不受阻（隔离卡死线程）"""
        engine, _ = make_engine(monkeypatch, False)
        await engine.initialize()
        old_executor = engine._executor
        assert old_executor is not None

        engine._model.behavior = 'hang'
        monkeypatch.setattr(engine, '_transcribe_timeout_s', lambda audio: 0.05)

        assert await engine.transcribe(audio()) is None  # 超时按失败处理
        assert engine._executor is None  # 已弃用，待懒重建

        # 新执行器可用：后续调用正常返回
        engine._model.behavior = 'ok'
        result = await engine.transcribe(audio())
        assert engine._executor is not None
        assert engine._executor is not old_executor  # 已替换
        assert result is not None
        assert result['text'] == 'hi'

    async def test_timeout_budget_scales_with_duration(self, monkeypatch):
        """预算公式：max(15, 3×时长+10)"""
        engine, _ = make_engine(monkeypatch, False)
        assert engine._transcribe_timeout_s(audio(1.0)) == pytest.approx(15.0)
        assert engine._transcribe_timeout_s(audio(10.0)) == pytest.approx(40.0)


class TestSerialization:
    async def test_concurrent_initialize_and_change_model_serialize(self, monkeypatch):
        """并发 initialize + 同值 change_model 串行化：单模型语义下幂等去重，无后写覆盖"""
        created = []
        gate = threading.Event()
        first = {'value': True}

        def fake_builder(provider):
            created.append(provider)
            if first['value']:
                first['value'] = False
                gate.wait(timeout=5)  # 卡住首次加载，制造并发窗口
            return FakeRecognizer(provider)

        engine, _ = make_engine(monkeypatch, True)
        monkeypatch.setattr(engine, '_build_recognizer', fake_builder)

        init_task = asyncio.create_task(engine.initialize())
        await asyncio.sleep(0.05)  # 让首次加载进入线程
        switch_task = asyncio.create_task(engine.change_model(engine.model_id))
        await asyncio.sleep(0.05)  # 切换等待锁
        gate.set()

        await asyncio.gather(init_task, switch_task)

        # initialize 完成后 change_model 同值幂等 → 仅一次加载，终态一致
        assert created == ['cuda']
        assert engine.model_id == 'funasr-nano'
        assert isinstance(engine._model, FakeRecognizer)
        assert engine.resolved_device == 'cuda'

    async def test_concurrent_change_model_dedupes_loads(self, monkeypatch):
        """两个并发 change_model（引擎未初始化）串行化：只加载一次"""
        created = []
        gate = threading.Event()
        first = {'value': True}

        def fake_builder(provider):
            created.append(provider)
            if first['value']:
                first['value'] = False
                gate.wait(timeout=5)
            return FakeRecognizer(provider)

        engine, _ = make_engine(monkeypatch, True)
        monkeypatch.setattr(engine, '_build_recognizer', fake_builder)

        t1 = asyncio.create_task(engine.change_model(engine.model_id))
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(engine.change_model(engine.model_id))
        await asyncio.sleep(0.05)
        gate.set()

        await asyncio.gather(t1, t2)

        assert created == ['cuda']  # 第二个请求锁内双检命中，不重复加载
        assert engine.is_ready


class TestSingleModelSemantics:
    """单引擎模型语义（spec: model-lifecycle「模型切换原子性」）"""

    async def test_change_model_same_value_idempotent(self, monkeypatch):
        engine, created = make_engine(monkeypatch, False)
        await engine.initialize()
        assert len(created) == 1

        await engine.change_model('funasr-nano')  # 同值幂等：不重载
        assert len(created) == 1
        assert engine.is_ready

    async def test_change_model_other_value_raises(self, monkeypatch):
        """旧档位名等其他值 → ValueError（调用方回执 invalid_model）"""
        engine, _ = make_engine(monkeypatch, False)
        await engine.initialize()
        for bad in ('tiny', 'base', 'small', 'medium', 'large-v3', 'nope'):
            with pytest.raises(ValueError):
                await engine.change_model(bad)

    async def test_invalid_config_model_falls_back_with_warning(self, caplog):
        """config asr.model 非法 → 回退引擎模型并告警"""
        caplog.set_level(logging.WARNING)
        engine = ASREngine({'asr': {'model': 'small'}})
        assert engine.model_id == 'funasr-nano'
        assert any('asr.model' in r.getMessage() for r in caplog.records)

    async def test_transcribe_returns_source_language_and_none_confidence(self, monkeypatch):
        engine, _ = make_engine(monkeypatch, False)
        await engine.initialize()
        result = await engine.transcribe(audio(0.1))
        assert result == {'text': 'hi', 'language': 'ja', 'confidence': None}


class TestSourceLanguage:
    """源语言链路（spec: language-handling「源语言唯一认定/取值范围」）"""

    def test_default_source_language_is_ja(self):
        engine = ASREngine({'asr': {}})
        assert engine.source_language == 'ja'

    def test_explicit_config_language(self):
        assert ASREngine({'asr': {'language': 'zh'}}).source_language == 'zh'
        assert ASREngine({'asr': {'language': 'EN'}}).source_language == 'en'

    def test_invalid_config_language_falls_back_with_warning(self, caplog):
        caplog.set_level(logging.WARNING)
        engine = ASREngine({'asr': {'language': 'fr'}})
        assert engine.source_language == 'ja'
        assert any('asr.language' in r.getMessage() for r in caplog.records)

    def test_language_hint_mapping(self):
        engine = ASREngine({'asr': {'language': 'zh'}})
        assert engine._language_hint() == '中文'
        engine.source_language = 'en'
        assert engine._language_hint() == '英文'
        engine.source_language = 'ja'
        assert engine._language_hint() == '日文'

    async def test_change_source_language_updates_state(self, monkeypatch):
        engine, _ = make_engine(monkeypatch, False)
        assert engine.change_source_language('zh') == 'zh'
        assert engine.source_language == 'zh'

    async def test_change_source_language_invalid_raises(self, monkeypatch):
        engine, _ = make_engine(monkeypatch, False)
        with pytest.raises(ValueError):
            engine.change_source_language('ko')
        with pytest.raises(ValueError):
            engine.change_source_language(None)
        assert engine.source_language == 'ja'  # 保持不变

    async def test_change_source_language_schedules_rebuild_when_ready(self, monkeypatch):
        """已初始化且语言变化 → 调度后台重建（识别器原子换入，服务不中断）"""
        engine, _ = make_engine(monkeypatch, False)
        await engine.initialize()
        old_model = engine._model
        assert engine._applied_language == 'ja'

        engine.change_source_language('zh')
        # 重建在途：is_ready 保持 True（不置 _switching，语句零丢失）
        assert engine.is_ready
        assert await wait_for(lambda: engine._applied_language == 'zh')
        assert engine._model is not old_model
        assert engine.is_ready

    async def test_change_source_language_same_value_no_rebuild(self, monkeypatch):
        engine, _ = make_engine(monkeypatch, False)
        await engine.initialize()
        old_model = engine._model

        engine.change_source_language('ja')  # 同值幂等
        await asyncio.sleep(0.05)
        assert engine._model is old_model

    async def test_rebuild_failure_keeps_old_recognizer(self, monkeypatch):
        """重建失败 → 保留旧识别器继续服务，不中断可用性"""
        engine, _ = make_engine(monkeypatch, False)
        await engine.initialize()
        old_model = engine._model

        def boom(provider):
            raise RuntimeError('rebuild failed')

        engine.change_source_language('en')
        engine._build_recognizer = boom  # 重建路径失败
        await asyncio.sleep(0.05)
        await asyncio.gather(engine._language_rebuild_task, return_exceptions=True)

        assert engine._model is old_model
        assert engine.is_ready
        assert engine.source_language == 'en'  # 状态保持新值

    async def test_not_initialized_change_does_not_schedule(self, monkeypatch):
        """未初始化：仅更新状态，不调度重建（下次 initialize 以新语言构建）"""
        engine, _ = make_engine(monkeypatch, False)
        engine.change_source_language('zh')
        assert engine.source_language == 'zh'
        assert engine._language_rebuild_task is None


class TestDecodeParams:
    """解码参数配置化（spec: audio-streaming「识别解码参数（Fun-ASR-Nano）」）"""

    def test_defaults(self):
        engine = ASREngine({'asr': {}})
        assert engine.itn is True
        assert engine.hotwords == ''
        assert engine.num_threads == 2

    def test_explicit_overrides(self):
        engine = ASREngine({
            'asr': {'itn': False, 'hotwords': '雪山,青梅', 'num_threads': 4}
        })
        assert engine.itn is False
        assert engine.hotwords == '雪山,青梅'
        assert engine.num_threads == 4

    def test_invalid_values_fall_back_with_warning(self, caplog):
        caplog.set_level(logging.WARNING)
        engine = ASREngine({
            'asr': {'itn': 'yes', 'hotwords': 123, 'num_threads': 0}
        })
        assert engine.itn is True
        assert engine.hotwords == ''
        assert engine.num_threads == 2
        messages = [r.getMessage() for r in caplog.records]
        assert any('itn' in m for m in messages)
        assert any('hotwords' in m for m in messages)
        assert any('num_threads' in m for m in messages)

    def test_builder_passes_configured_params(self, monkeypatch):
        """识别器构造以配置参数调用底层 API（language 提示/itn/hotwords/threads）"""
        captured = {}

        def fake_builder(provider):
            captured['provider'] = provider
            return FakeRecognizer(provider)

        engine, _ = make_engine(
            monkeypatch, False,
            config={'itn': False, 'hotwords': 'A,B', 'num_threads': 3,
                    'language': 'zh'},
        )
        monkeypatch.setattr(engine, '_build_recognizer', fake_builder)
        # 直接以真实构造参数语义验证：还原真实 _build_recognizer 检查不方便，
        # 改为验证 sherpa 调用参数——monkeypatch from_funasr_nano
        import sherpa_onnx

        def fake_from_funasr_nano(**kwargs):
            captured.update(kwargs)
            return FakeRecognizer(kwargs.get('provider', 'cpu'))

        monkeypatch.setattr(
            sherpa_onnx.OfflineRecognizer, 'from_funasr_nano',
            staticmethod(fake_from_funasr_nano),
        )
        real_builder = ASREngine._build_recognizer
        real_builder(engine, 'cpu')

        assert captured['provider'] == 'cpu'
        assert captured['language'] == '中文'
        assert captured['itn'] is False
        assert captured['hotwords'] == 'A,B'
        assert captured['num_threads'] == 3


class TestBundledCudaDllPath:
    """随包 CUDA 运行库 PATH 注入（spec: model-lifecycle「随包 CUDA 运行库加载」）"""

    def test_injects_dir_into_path_once(self, tmp_path, monkeypatch):
        """目录存在 → 前置到 PATH 且幂等（不重复）"""
        cuda_dir = tmp_path / 'win-x64-cuda'
        cuda_dir.mkdir()
        (cuda_dir / 'cublas64_12.dll').write_bytes(b'fake')
        monkeypatch.setenv('PATH', r'C:\base')

        asr_mod.ensure_bundled_cuda_dll_path(root=tmp_path)
        asr_mod.ensure_bundled_cuda_dll_path(root=tmp_path)  # 幂等：第二次跳过

        parts = os.environ['PATH'].split(os.pathsep)
        resolved = str(cuda_dir.resolve())
        assert parts[0] == resolved
        assert parts.count(resolved) == 1
        assert r'C:\base' in parts

    def test_missing_dir_is_noop(self, tmp_path, monkeypatch):
        """随包目录缺失 → PATH 不变、不抛错（行为与变更前一致）"""
        monkeypatch.setenv('PATH', r'C:\base')
        asr_mod.ensure_bundled_cuda_dll_path(root=tmp_path)  # 无 win-x64-cuda 子目录
        assert os.environ['PATH'] == r'C:\base'

    def test_non_windows_is_noop(self, tmp_path, monkeypatch):
        """非 Windows 平台 → 静默跳过（无副作用）"""
        (tmp_path / 'win-x64-cuda').mkdir()
        monkeypatch.setenv('PATH', r'C:\base')
        monkeypatch.setattr(asr_mod, '_IS_WINDOWS', False)
        asr_mod.ensure_bundled_cuda_dll_path(root=tmp_path)
        assert os.environ['PATH'] == r'C:\base'

    def test_swallows_resolve_errors(self, monkeypatch):
        """目录解析异常 → 吞掉不外溢"""
        import llama_server_manager as lsm

        def boom():
            raise RuntimeError('boom')

        monkeypatch.setattr(lsm, 'resolve_vendor_root', boom)
        asr_mod.ensure_bundled_cuda_dll_path(root=None)  # 不抛异常即通过

    async def test_load_with_fallback_invokes_injection(self, monkeypatch):
        """统一加载路径（含重载共用）须调用注入"""
        called = []
        monkeypatch.setattr(
            asr_mod, 'ensure_bundled_cuda_dll_path', lambda: called.append(1)
        )

        engine, _ = make_engine(monkeypatch, False)  # 无 CUDA → CPU 路径
        await engine.initialize()
        assert called  # 注入在加载路径上被执行（函数内部自判平台/目录）
