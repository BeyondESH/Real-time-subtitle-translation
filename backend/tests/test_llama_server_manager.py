"""llama_server_manager 单元测试：启停 / 健康超时 / 崩溃重启 / 端口重试 / 清理 / 探针解析

全部使用注入的 fake spawn/health，不依赖真实进程与网络。
"""
import asyncio
import subprocess
import time
from pathlib import Path

import llama_server_manager as lsm
from llama_server_manager import LlamaServerManager, ServerConfig


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeProc:
    """模拟 subprocess.Popen 的最小接口"""

    def __init__(self, *, alive: bool = True, terminate_effective: bool = True,
                 wait_raises_timeout: bool = False):
        self._rc = None if alive else 1
        self.terminate_effective = terminate_effective
        self.wait_raises_timeout = wait_raises_timeout
        self.terminate_called = False
        self.kill_called = False

    def poll(self):
        return self._rc

    def simulate_exit(self, rc: int = 1):
        self._rc = rc

    def terminate(self):
        self.terminate_called = True
        if self.terminate_effective:
            self._rc = -15

    def kill(self):
        self.kill_called = True
        self._rc = -9

    def wait(self, timeout=None):
        if self._rc is not None:
            return self._rc
        if self.wait_raises_timeout:
            raise subprocess.TimeoutExpired('llama-server', timeout)
        self._rc = 0
        return self._rc


class FakeSpawner:
    """按序返回预设进程，并记录每次 spawn 的端口"""

    def __init__(self, procs):
        self.procs = list(procs)
        self.ports = []
        self.configs = []

    def __call__(self, config, port, log_file):
        self.ports.append(port)
        self.configs.append(config)
        return self.procs.pop(0)


def _config(tmp_path: Path) -> ServerConfig:
    return ServerConfig(
        exe_path=tmp_path / 'llama-server.exe',
        model_path=tmp_path / 'model.gguf',
        n_ctx=4096,
        n_gpu_layers=99,
    )


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _manager(tmp_path, spawner, health, **kwargs) -> LlamaServerManager:
    defaults = dict(
        health_poll_interval_s=0.005,
        supervise_interval_s=0.01,
        backoff_s=(0.01, 0.01, 0.01),
        max_restarts=3,
    )
    defaults.update(kwargs)
    return LlamaServerManager(
        log_dir=tmp_path,
        spawn_fn=spawner,
        health_fn=health,
        **defaults,
    )


HEALTH_OK = lambda: (lambda port: asyncio.sleep(0, result=True))


# --------------------------------------------------------------------------- #
# 启停
# --------------------------------------------------------------------------- #

async def test_start_and_stop(tmp_path):
    procs = [FakeProc()]
    spawner = FakeSpawner(procs)
    manager = _manager(tmp_path, spawner, HEALTH_OK())

    await manager.start(_config(tmp_path))
    assert manager.is_ready and manager.is_running
    assert manager.port == spawner.ports[0] > 0

    await manager.stop()
    assert not manager.is_running
    assert not manager.is_ready
    assert spawner.procs == [] and procs[0].terminate_called


async def test_binary_missing_raises(tmp_path):
    def spawn_raise(config, port, log_file):
        raise FileNotFoundError(config.exe_path)

    manager = _manager(tmp_path, spawn_raise, HEALTH_OK())
    try:
        await manager.start(_config(tmp_path))
        assert False, '应当抛出 RuntimeError'
    except RuntimeError as e:
        assert '二进制不存在' in str(e)
    assert not manager.is_running


async def test_health_timeout_raises_and_cleans(tmp_path):
    async def never_healthy(port):
        return False

    proc = FakeProc()
    manager = _manager(
        tmp_path, FakeSpawner([proc]), never_healthy, health_timeout_s=0.05
    )
    try:
        await manager.start(_config(tmp_path))
        assert False, '应当抛出 RuntimeError'
    except RuntimeError as e:
        assert '启动失败' in str(e)
    assert proc.terminate_called
    assert not manager.is_ready


# --------------------------------------------------------------------------- #
# 崩溃与重启
# --------------------------------------------------------------------------- #

async def test_crash_restarts_with_backoff(tmp_path):
    first, second = FakeProc(), FakeProc()
    spawner = FakeSpawner([first, second])
    events = []
    manager = _manager(
        tmp_path, spawner, HEALTH_OK(),
        on_event=lambda k, d: events.append((k, d)),
    )

    await manager.start(_config(tmp_path))
    assert manager.is_ready

    first.simulate_exit(1)
    assert await _wait_for(lambda: manager.is_ready and len(spawner.ports) == 2), \
        '应在崩溃后自动重启'
    kinds = [k for k, _ in events]
    assert 'exit' in kinds and 'restarted' in kinds
    assert spawner.ports[0] != spawner.ports[1], '重启应重新分配端口'

    await manager.stop()


async def test_gave_up_after_max_restarts(tmp_path):
    first = FakeProc()
    # 重启尝试时进程一直存活但健康检查不再通过 → 每次超时后失败重试
    restart_procs = [FakeProc() for _ in range(4)]
    spawner = FakeSpawner([first] + restart_procs)
    calls = {'n': 0}

    async def flaky_health(port):
        calls['n'] += 1
        return calls['n'] == 1  # 仅首次启动健康

    events = []
    manager = _manager(
        tmp_path, spawner, flaky_health,
        health_timeout_s=0.05, max_restarts=2,
        on_event=lambda k, d: events.append((k, d)),
    )

    await manager.start(_config(tmp_path))
    first.simulate_exit(1)

    assert await _wait_for(lambda: any(k == 'gave_up' for k, _ in events)), \
        '重启达上限应回调 gave_up'
    assert len(spawner.ports) >= 3  # 初次 + 2 次重启尝试
    assert not manager.is_ready

    await manager.stop()


async def test_port_retry_when_process_dies_immediately(tmp_path):
    """首次 spawn 的进程立即退出（如端口冲突），应换端口重试成功"""
    dead, alive = FakeProc(alive=False), FakeProc()
    spawner = FakeSpawner([dead, alive])
    manager = _manager(tmp_path, spawner, HEALTH_OK())

    await manager.start(_config(tmp_path))
    assert manager.is_ready
    assert len(spawner.ports) == 2
    assert spawner.ports[0] != spawner.ports[1]

    await manager.stop()


# --------------------------------------------------------------------------- #
# 终止兜底
# --------------------------------------------------------------------------- #

async def test_terminate_falls_back_to_kill(tmp_path):
    proc = FakeProc(terminate_effective=False, wait_raises_timeout=True)
    manager = _manager(tmp_path, FakeSpawner([proc]), HEALTH_OK())

    await manager.start(_config(tmp_path))
    await manager.stop()

    assert proc.terminate_called and proc.kill_called
    assert not manager.is_running


# --------------------------------------------------------------------------- #
# 探针解析
# --------------------------------------------------------------------------- #

def test_list_devices_parses_cuda(tmp_path, monkeypatch):
    sample = (
        "Available devices:\n"
        "  CUDA0: NVIDIA GeForce RTX 5060 (8123 MiB, 7031 MiB free)\n"
    )

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=sample, stderr='')

    monkeypatch.setattr(lsm.subprocess, 'run', fake_run)
    devices = lsm.list_devices(tmp_path / 'llama-server.exe')
    assert devices == ['CUDA0: NVIDIA GeForce RTX 5060 (8123 MiB, 7031 MiB free)']


def test_list_devices_none_and_error(tmp_path, monkeypatch):
    def none_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout='Available devices:\n  (none)\n', stderr=''
        )

    monkeypatch.setattr(lsm.subprocess, 'run', none_run)
    assert lsm.list_devices(tmp_path / 'x.exe') == []

    def boom(args, **kwargs):
        raise FileNotFoundError('missing')

    monkeypatch.setattr(lsm.subprocess, 'run', boom)
    assert lsm.list_devices(tmp_path / 'x.exe') == []
