"""
llama-server 进程管理器（翻译引擎运行时）

职责：以子进程方式拉起随包的 llama-server（llama.cpp），并提供
- 空闲端口分配与启动参数构造（设备 offload / 上下文 / profile 附加参数）
- 启动就绪与运行期健康检查
- 异常退出后的指数退避重启（达上限后回调 gave_up，由上层决定降级/告警）
- 终止清理（terminate → wait → kill 兜底），保证无残留进程

设计约束（design.md D1/D2）：
- 同一时刻至多一个 llama-server 进程（LRU=1，模型切换 = 终止 + 重启）
- 进程级隔离：推理崩溃不拖垮后端；显存随进程终止彻底释放
- 可注入 spawn_fn / health_fn 以便单元测试（不依赖真实进程与网络）
"""
import asyncio
import logging
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Dict, IO, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# 健康检查轮询周期（秒）
_HEALTH_POLL_INTERVAL_S = 0.5
# 单次 /health HTTP 请求超时（秒）
_HEALTH_HTTP_TIMEOUT_S = 2.0
# 启动就绪总预算（秒）：1.8B 模型加载应远小于此
_DEFAULT_HEALTH_TIMEOUT_S = 90.0
# 运行期存活巡检周期（秒）
_DEFAULT_SUPERVISE_INTERVAL_S = 1.0
# 异常退出后的指数退避序列（秒）
_DEFAULT_BACKOFF_S: Tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
# 重启尝试上限（超过后回调 gave_up）
_DEFAULT_MAX_RESTARTS = 5
# 端口冲突/进程猝死时的换端口重试次数
_PORT_RETRIES = 2
# 终止等待预算（秒）
_TERMINATE_WAIT_S = 5.0
_KILL_WAIT_S = 3.0

EventCallback = Callable[[str, Dict], None]
SpawnFn = Callable[["ServerConfig", int, IO[bytes]], object]
HealthFn = Callable[[int], Awaitable[bool]]


@dataclass
class ServerConfig:
    """单次 llama-server 启动配置（切换模型/设备即换一份配置重启）"""

    exe_path: Path
    model_path: Path
    n_ctx: int = 4096
    n_gpu_layers: int = 0  # 99=全部 offload；0=纯 CPU
    threads: int = 0  # 0=llama.cpp 默认
    extra_args: Tuple[str, ...] = field(default_factory=tuple)


def resolve_vendor_root() -> Path:
    """
    llama 二进制根目录：`SUBTITLE_LLAMA_DIR`（打包时由 Electron 注入）优先，
    否则回落到仓库内 `backend/vendor/llama`（开发模式）。
    """
    env = os.environ.get('SUBTITLE_LLAMA_DIR', '').strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / 'vendor' / 'llama'


def resolve_device_binary(root: Optional[Path], device: str) -> Path:
    """按设备解析 llama-server 可执行文件路径（cuda → CUDA 构建，其余 → CPU 构建）"""
    base = root if root is not None else resolve_vendor_root()
    variant = 'win-x64-cuda' if device == 'cuda' else 'win-x64-cpu'
    return base / variant / 'llama-server.exe'


def resolve_log_dir() -> Path:
    """日志目录（与 main.py 的 SUBTITLE_LOG_DIR 约定一致）"""
    return Path(
        os.environ.get(
            'SUBTITLE_LOG_DIR',
            Path.home() / '.cache' / 'subtitle-translator' / 'logs'
        )
    )


# 启动期遗留进程回收：每进程仅执行一次
_stale_reaped = False


def kill_stale_llama_servers() -> int:
    """
    回收【上次进程非正常退出】遗留的 llama-server 进程（按可执行路径过滤为本应用
    vendor 目录下的实例，MUST NOT 误伤其它来源的同名进程）。

    - Windows-only；幂等（每进程仅执行一次）；任何失败静默跳过（MUST NOT 影响启动）
    - 背景：应用被强杀/崩溃时 BackendManager 的 taskkill 进程树清理可能未执行到位，
      遗留进程持续占用显存（实测多个遗留实例可将 LLM 生成速度从 ~200 tok/s 拖至 47 tok/s，
      并挤压 ASR GPU 加载）

    Returns:
        实际清理的进程数（无法判定时 0）
    """
    global _stale_reaped
    if os.name != 'nt':
        return 0
    if _stale_reaped:
        return 0
    _stale_reaped = True
    try:
        vendor = str(resolve_vendor_root().resolve())
        script = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "Get-CimInstance Win32_Process -Filter \"Name='llama-server.exe'\" | "
            f"Where-Object {{ $_.ExecutablePath -like '{vendor}*' }} | "
            "ForEach-Object { Write-Output $_.ProcessId; Stop-Process -Id $_.ProcessId -Force }"
        )
        out = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        killed = [tok for tok in (out.stdout or '').split() if tok.strip().isdigit()]
        if killed:
            logger.warning(
                "启动回收：已清理上次遗留的 llama-server 进程 %s", ','.join(killed)
            )
        return len(killed)
    except Exception as e:  # noqa: BLE001 - 回收失败静默，不影响启动语义
        logger.debug("遗留 llama-server 回收失败（忽略）: %s", e)
        return 0


def pick_free_port() -> int:
    """向系统申请一个空闲回环端口（bind 0）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return int(s.getsockname()[1])


def list_devices(exe_path: Path, timeout_s: float = 15.0) -> List[str]:
    """
    运行 `llama-server --list-devices` 并解析设备行（用于设备探针）。

    Returns:
        设备描述行列表（如 ['CUDA0: NVIDIA GeForce RTX 5060 (8123 MiB, 7031 MiB free)']）；
        二进制缺失/执行异常/超时一律返回空列表（探针语义：不可用）。
    """
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        proc = subprocess.run(
            [str(exe_path), '--list-devices'],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=creationflags,
        )
    except Exception as e:  # noqa: BLE001 - 探针异常一律降为不可用
        logger.debug("llama --list-devices 不可用: %s", e)
        return []

    devices: List[str] = []
    for line in ((proc.stdout or '') + '\n' + (proc.stderr or '')).splitlines():
        s = line.strip()
        if not s or s.startswith('Available devices') or s.startswith('(none)'):
            continue
        if ':' in s:
            devices.append(s)
    return devices


def _default_spawn(config: ServerConfig, port: int, log_file: IO[bytes]):
    """默认 spawn：直接拉起 llama-server，标准输出合并写入日志文件（句柄由管理器管理）"""
    args = [
        str(config.exe_path),
        '--model', str(config.model_path),
        '--host', '127.0.0.1',
        '--port', str(port),
        '-ngl', str(config.n_gpu_layers),
        '-c', str(config.n_ctx),
    ]
    if config.threads and config.threads > 0:
        args += ['-t', str(config.threads)]
    args += list(config.extra_args)

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    return subprocess.Popen(
        args,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        cwd=str(config.exe_path.parent),
    )


class LlamaServerManager:
    """
    llama-server 生命周期管理（单实例）。

    on_event(kind, detail) 事件：
    - 'exit'      异常退出（returncode）
    - 'restarting' 重启尝试开始（attempt/delay_s）
    - 'restarted'  重启成功（attempt）
    - 'gave_up'    重启达上限，放弃（attempts）
    事件在事件循环上下文触发；回调异常不得外溢。
    """

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        on_event: Optional[EventCallback] = None,
        *,
        spawn_fn: Optional[SpawnFn] = None,
        health_fn: Optional[HealthFn] = None,
        health_timeout_s: float = _DEFAULT_HEALTH_TIMEOUT_S,
        health_poll_interval_s: float = _HEALTH_POLL_INTERVAL_S,
        supervise_interval_s: float = _DEFAULT_SUPERVISE_INTERVAL_S,
        backoff_s: Tuple[float, ...] = _DEFAULT_BACKOFF_S,
        max_restarts: int = _DEFAULT_MAX_RESTARTS,
    ):
        self._log_dir = Path(log_dir) if log_dir is not None else resolve_log_dir()
        self._on_event = on_event
        self._spawn_fn = spawn_fn or _default_spawn
        self._health_fn = health_fn
        self._health_timeout_s = health_timeout_s
        self._health_poll_interval_s = health_poll_interval_s
        self._supervise_interval_s = supervise_interval_s
        self._backoff_s = tuple(backoff_s)
        self._max_restarts = max_restarts

        self._proc = None
        self._config: Optional[ServerConfig] = None
        self._port: Optional[int] = None
        self._ready = False
        self._stop_requested = False
        self._restart_attempts = 0
        self._lock = asyncio.Lock()  # 串行化 start/stop/重启
        self._monitor_task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._log_file = None

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def port(self) -> Optional[int]:
        return self._port

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def is_ready(self) -> bool:
        return self._ready and self.is_running

    @property
    def model_path(self) -> Optional[Path]:
        return self._config.model_path if self._config else None

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #

    async def start(self, config: ServerConfig) -> None:
        """
        启动 llama-server 并等待健康就绪。

        Raises:
            RuntimeError: 二进制缺失 / 健康检查超时 / 启动连续失败（资源已清理）
        """
        # 首次启动：回收上次进程非正常退出遗留的实例（幂等；非 Windows/失败 no-op）
        kill_stale_llama_servers()
        async with self._lock:
            self._stop_requested = False
            self._restart_attempts = 0
            await self._start_locked(config)
        self._ensure_monitor()

    async def stop(self) -> None:
        """终止当前进程并停止巡逻（幂等）"""
        self._stop_requested = True
        task = self._monitor_task
        self._monitor_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            await self._terminate_locked()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不影响停止语义
                logger.debug("关闭健康检查客户端失败（忽略）", exc_info=True)
            self._client = None

    async def _start_locked(self, config: ServerConfig) -> None:
        """锁内启动：换端口重试 → 拉起 → 健康等待；失败清理并抛错"""
        await self._terminate_locked()
        self._config = config
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / 'llama-server.log'

        last_error = ''
        for attempt in range(_PORT_RETRIES):
            port = pick_free_port()
            self._port = port
            log_file = open(log_path, 'ab')  # noqa: SIM115 - 句柄由 _close_log_file 关闭
            self._log_file = log_file
            try:
                self._proc = self._spawn_fn(config, port, log_file)
            except FileNotFoundError as e:
                self._close_log_file()
                raise RuntimeError(f"llama-server 二进制不存在: {config.exe_path}") from e
            except Exception:
                self._close_log_file()
                raise

            ok = await self._wait_healthy(port)
            if ok:
                self._ready = True
                logger.info(
                    "llama-server 就绪（端口 %d，模型 %s，ngl=%d）",
                    port, config.model_path.name, config.n_gpu_layers,
                )
                return

            rc = self._proc.poll() if self._proc is not None else None
            logger.warning(
                "llama-server 启动未就绪（尝试 %d/%d，端口 %d，退出码 %s）",
                attempt + 1, _PORT_RETRIES, port, rc,
            )
            last_error = f"健康检查未通过（退出码 {rc}）"
            await self._terminate_locked()
            if rc is None:
                # 进程仍存活但健康超时（如模型加载过慢）：不再换端口
                break

        raise RuntimeError(f"llama-server 启动失败: {last_error or '未知原因'}")

    async def _wait_healthy(self, port: int) -> bool:
        """轮询 /health 直至 200；进程提前退出立即返回 False"""
        deadline = time.monotonic() + self._health_timeout_s
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            try:
                if await self._check_health(port):
                    return True
            except Exception:  # noqa: BLE001 - 启动期连接失败属预期
                pass
            await asyncio.sleep(self._health_poll_interval_s)
        return False

    async def _check_health(self, port: int) -> bool:
        """健康检查（默认经 httpx GET /health；测试可注入 health_fn）"""
        if self._health_fn is not None:
            return await self._health_fn(port)
        if self._client is None:
            self._client = httpx.AsyncClient()
        resp = await self._client.get(
            f"http://127.0.0.1:{port}/health", timeout=_HEALTH_HTTP_TIMEOUT_S
        )
        return resp.status_code == 200

    async def _terminate_locked(self) -> None:
        """终止当前进程：terminate → wait → kill；关闭日志句柄（幂等）"""
        proc = self._proc
        self._proc = None
        self._ready = False
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                logger.debug("terminate 失败（忽略）", exc_info=True)
            alive = True
            try:
                await asyncio.to_thread(proc.wait, _TERMINATE_WAIT_S)
                alive = proc.poll() is None
            except Exception:  # noqa: BLE001 - 含 TimeoutExpired
                alive = proc.poll() is None
            if alive:
                logger.warning("llama-server 未按期退出，强制 kill")
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    logger.debug("kill 失败（忽略）", exc_info=True)
                try:
                    await asyncio.to_thread(proc.wait, _KILL_WAIT_S)
                except Exception:  # noqa: BLE001
                    logger.debug("kill 后等待超时（忽略）", exc_info=True)
        self._close_log_file()

    def _close_log_file(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭 llama-server 日志句柄失败（忽略）", exc_info=True)
            self._log_file = None

    # ------------------------------------------------------------------ #
    # 巡逻与自动重启
    # ------------------------------------------------------------------ #

    def _ensure_monitor(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self) -> None:
        """运行期存活巡检：发现异常退出 → 指数退避重启"""
        while True:
            await asyncio.sleep(self._supervise_interval_s)
            if self._stop_requested:
                return
            proc = self._proc
            if proc is None:
                return
            rc = proc.poll()
            if rc is None:
                continue
            await self._handle_unexpected_exit(rc)

    async def _handle_unexpected_exit(self, returncode: int) -> None:
        """异常退出处置：清理 → 退避重试（不长时间持锁，stop 可立即生效）"""
        async with self._lock:
            if (
                self._stop_requested
                or self._proc is None
                or self._proc.poll() is None
            ):
                return  # 已被 stop()/start() 处理
            self._ready = False
            logger.warning("llama-server 异常退出（退出码 %s），进入重启", returncode)
            await self._terminate_locked()
            self._emit('exit', {'returncode': returncode})

        config = self._config
        if config is None:
            return

        while (
            not self._stop_requested
            and self._restart_attempts < self._max_restarts
        ):
            delay = self._backoff_s[
                min(self._restart_attempts, len(self._backoff_s) - 1)
            ]
            self._restart_attempts += 1
            self._emit('restarting', {
                'attempt': self._restart_attempts, 'delay_s': delay,
            })
            await asyncio.sleep(delay)
            if self._stop_requested:
                return
            try:
                async with self._lock:
                    if self._stop_requested:
                        return
                    await self._start_locked(config)
            except Exception as e:  # noqa: BLE001 - 重启失败继续退避
                logger.warning(
                    "llama-server 重启失败（第 %d 次）: %s",
                    self._restart_attempts, e,
                )
                continue
            logger.info("llama-server 重启成功（第 %d 次尝试）", self._restart_attempts)
            attempt = self._restart_attempts
            self._restart_attempts = 0
            self._emit('restarted', {'attempt': attempt})
            return

        if not self._stop_requested:
            logger.error(
                "llama-server 重启次数达上限（%d），放弃", self._max_restarts
            )
            self._emit('gave_up', {'attempts': self._max_restarts})

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    def _emit(self, kind: str, detail: Dict) -> None:
        callback = self._on_event
        if callback is None:
            return
        try:
            callback(kind, detail)
        except Exception:  # noqa: BLE001 - 回调异常不得影响管理循环
            logger.exception("llama-server 事件回调异常")
