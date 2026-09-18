"""
ASREngine 运行期健壮性测试（假加载器/假探针，无 GPU/网络）

覆盖 fix-asr-runtime-stall（任务 1.6）：
- 热身失败 → CPU 重载且 reason=runtime_failed（加载成功 ≠ 可推理）
- 运行期异常 → 一次性降级（防抖，不重复重载）
- 推理超时 → 执行器被替换且后续调用不受阻（隔离卡死线程）
- 并发 initialize/change_model 串行化（只加载一次、终态一致、无后写覆盖）
- notify_stall 仅在 cuda 时触发降级；CPU 连续失败仅广播一次
"""
import asyncio
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
        'source': 'ctranslate2',
        'detail': f'test probe cuda={cuda_available}',
    }


class FakeModel:
    """可编程假 Whisper 模型：behavior=ok/raise/hang"""

    def __init__(self, device, behavior='ok'):
        self.device = device
        self.behavior = behavior
        self.calls = 0

    def transcribe(self, audio, **kwargs):
        self.calls += 1
        if self.behavior == 'raise':
            raise RuntimeError(
                'Library cublas64_12.dll is not found or cannot be loaded'
            )
        if self.behavior == 'hang':
            time.sleep(1.0)  # 模拟卡死调用（由超时隔离；保持有界便于测试退出）
        info = types.SimpleNamespace(language='en', language_probability=1.0)
        segment = types.SimpleNamespace(text='hi')
        return iter((segment,)), info


def make_engine(monkeypatch, cuda_available, factory, device='auto',
                model_size='tiny'):
    """构造引擎：monkeypatch WhisperModel 与探针"""
    monkeypatch.setattr(asr_mod, 'WhisperModel', factory)
    monkeypatch.setattr(
        asr_mod, 'probe_compute', lambda: {'asr': _probe(cuda_available)}
    )
    return ASREngine({
        'asr': {'model_size': model_size, 'device': device, 'compute_type': 'float16'}
    })


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
        """加载成功但推理不可用（缺 cuBLAS）→ CPU 重载 + runtime_failed"""
        created = []

        def factory(model_size, device, compute_type, download_root):
            created.append((device, compute_type))
            # cuda "加载成功" 但推理报错；cpu 正常
            return FakeModel(device, behavior='raise' if device == 'cuda' else 'ok')

        engine = make_engine(monkeypatch, True, factory)
        await engine.initialize()  # MUST NOT raise

        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'runtime_failed'
        assert created == [('cuda', 'float16'), ('cpu', 'int8')]

    async def test_warm_up_failure_on_cpu_bubbles(self, monkeypatch):
        """CPU 端到端验证同样失败 → 初始化失败语义（冒泡）"""
        def factory(model_size, device, compute_type, download_root):
            return FakeModel(device, behavior='raise')

        engine = make_engine(monkeypatch, False, factory)
        with pytest.raises(RuntimeError):
            await engine.initialize()

    async def test_warm_up_success_keeps_gpu(self, monkeypatch):
        """热身通过：GPU 路径保持（回归）"""
        created = []

        def factory(model_size, device, compute_type, download_root):
            created.append((device, compute_type))
            return FakeModel(device)

        engine = make_engine(monkeypatch, True, factory)
        await engine.initialize()

        assert engine.resolved_device == 'cuda'
        assert engine.device_reason == 'auto'
        assert created == [('cuda', 'float16')]


class TestRuntimeDegrade:
    async def test_runtime_failure_degrades_once(self, monkeypatch):
        """运行期异常 → 一次性降级；再次失败/停滞通知不重复重载"""
        models = {}
        created = []
        events = []

        def factory(model_size, device, compute_type, download_root):
            created.append(device)
            model = FakeModel(device)
            models[device] = model
            return model

        engine = make_engine(monkeypatch, True, factory)
        engine.set_health_callback(events.append)
        await engine.initialize()
        assert engine.resolved_device == 'cuda'

        # 运行期推理失败 → 调度降级
        models['cuda'].behavior = 'raise'
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
        created = []

        def factory(model_size, device, compute_type, download_root):
            created.append(device)
            return FakeModel(device)

        engine = make_engine(monkeypatch, False, factory)
        await engine.initialize()
        assert engine.resolved_device == 'cpu'

        engine.notify_stall()
        await asyncio.sleep(0.05)
        assert created == ['cpu']

    async def test_cpu_persistent_failure_notified_once(self, monkeypatch):
        """CPU 连续失败 ≥3 次仅广播一次（不刷屏、不无界重试）"""
        events = []

        def factory(model_size, device, compute_type, download_root):
            return FakeModel(device, behavior='raise')

        engine = make_engine(monkeypatch, False, factory)
        engine.set_health_callback(events.append)
        # 直接进入初始化会因热身失败冒泡 → 手工模拟已初始化状态
        engine._initialized = True
        engine._model = FakeModel('cpu', behavior='raise')
        engine._resolved_device = 'cpu'
        engine._device_reason = 'user'

        for _ in range(4):
            assert await engine.transcribe(audio()) is None

        assert [e['event'] for e in events] == ['persistent_failure']
        assert events[0]['failures'] == 3

    async def test_success_resets_failure_streak(self, monkeypatch):
        """成功一次即清零连续失败计数"""
        events = []

        def factory(model_size, device, compute_type, download_root):
            return FakeModel(device)

        engine = make_engine(monkeypatch, False, factory)
        engine.set_health_callback(events.append)
        engine._initialized = True
        engine._model = FakeModel('cpu')
        engine._resolved_device = 'cpu'
        engine._device_reason = 'user'

        engine._model.behavior = 'raise'
        assert await engine.transcribe(audio()) is None
        assert await engine.transcribe(audio()) is None
        engine._model.behavior = 'ok'
        assert await engine.transcribe(audio()) is not None  # 成功 → 清零
        engine._model.behavior = 'raise'
        assert await engine.transcribe(audio()) is None
        assert await engine.transcribe(audio()) is None
        assert await engine.transcribe(audio()) is None  # 重新数到第 3 次

        assert [e['event'] for e in events] == ['persistent_failure']


class TestTimeoutIsolation:
    async def test_timeout_resets_executor_and_next_call_works(self, monkeypatch):
        """超时 → 执行器被弃用替换；后续调用不受阻（隔离卡死线程）"""
        def factory(model_size, device, compute_type, download_root):
            return FakeModel(device)

        engine = make_engine(monkeypatch, False, factory)
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
        def factory(model_size, device, compute_type, download_root):
            return FakeModel(device)

        engine = make_engine(monkeypatch, False, factory)
        assert engine._transcribe_timeout_s(audio(1.0)) == pytest.approx(15.0)
        assert engine._transcribe_timeout_s(audio(10.0)) == pytest.approx(40.0)


class TestSerialization:
    async def test_concurrent_initialize_and_change_model_serialize(self, monkeypatch):
        """启动加载与并发切换串行化：只按最终模型加载一次，无后写覆盖"""
        created = []
        models = {}
        release = threading.Event()
        gate = {'first': True}

        def factory(model_size, device, compute_type, download_root):
            created.append((model_size, device))
            if gate['first']:
                gate['first'] = False
                release.wait(timeout=5)  # 卡住首次加载，制造并发窗口
            model = FakeModel(device)
            models[model_size] = model
            return model

        engine = make_engine(monkeypatch, True, factory, model_size='base')

        init_task = asyncio.create_task(engine.initialize())
        await asyncio.sleep(0.05)  # 让 base 加载进入线程
        switch_task = asyncio.create_task(engine.change_model('tiny'))
        await asyncio.sleep(0.05)  # 切换等待锁
        release.set()

        await asyncio.gather(init_task, switch_task)

        assert created == [('base', 'cuda'), ('tiny', 'cuda')]
        assert engine.model_size == 'tiny'
        assert engine._model is models['tiny']  # 终态一致，无覆盖
        assert engine.resolved_device == 'cuda'
