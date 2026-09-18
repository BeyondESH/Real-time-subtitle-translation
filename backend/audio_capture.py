"""
音频捕获模块 - 双通道音频源（设备回环 / 按进程回环）

- 设备级：soundcard WASAPI 回环捕获（默认源，行为与既有版本一致）。
- 进程级：``process_loopback.ProcessLoopbackCapture`` 捕获目标进程树（含子进程），
  以 48kHz/stereo/float32 原始帧回调，经既有 ``_process_audio`` 单声道化 +
  soxr 重采样至 16kHz，回调契约与设备源完全一致。

回调为同步函数，直接在录音线程中被调用（只写环形缓冲，不做重活）。
暂停期间继续读取音频流但直接丢弃，避免 WASAPI 缓冲区溢出。
"""
import asyncio
import logging
import threading
from typing import Callable, List, Optional

import numpy as np
import psutil
import soundcard as sc
import soxr

from process_loopback import (
    ProcessLoopbackCapture,
    supported as process_loopback_supported,
)

logger = logging.getLogger(__name__)

# 音频数据回调类型：同步函数，参数为 float32 单声道 16kHz 音频
AudioCallback = Callable[[np.ndarray], None]

# 进程回环固定交付格式（模块请求 48kHz/stereo/float32）
PROCESS_SAMPLE_RATE = 48000

# watchdog 轮询周期（秒）；create_time 容差用于 PID 复用判定
WATCHDOG_INTERVAL_S = 2.0
_CREATE_TIME_TOLERANCE = 1e-6


class AudioCapture:
    """WASAPI 音频捕获类"""

    def __init__(self, config: dict):
        self.config = config.get('audio', {})
        self.sample_rate = self.config.get('sample_rate', 16000)
        self.channels = self.config.get('channels', 1)
        self.chunk_size = self.config.get('chunk_size', 1024)

        self._microphone: Optional[sc.Microphone] = None
        self._recorder: Optional[sc.Recorder] = None
        self._callback: Optional[AudioCallback] = None
        self._running = False
        self._paused = False
        self._record_task: Optional[asyncio.Task] = None

        # 音频源状态：device（默认）/ process
        self._source_kind: str = 'device'
        self._device_id: str = ''
        self._process_pid: Optional[int] = None
        self._process_name: Optional[str] = None
        self._process_create_time: Optional[float] = None
        self._process_capture: Optional[ProcessLoopbackCapture] = None

        # 进程源 watchdog（暂停期间保持存活）
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop: Optional[threading.Event] = None
        self._watchdog_interval_s: float = WATCHDOG_INTERVAL_S
        self._on_source_lost: Optional[Callable[[str, int], None]] = None

    def get_audio_sources(self) -> List[dict]:
        """
        获取可用的音频源列表

        Returns:
            音频源信息列表，包含 id, name, is_loopback 等
        """
        sources = []

        # 获取所有麦克风（包括回环设备）
        microphones = sc.all_microphones(include_loopback=True)

        for mic in microphones:
            source_info = {
                'id': mic.id,
                'name': mic.name,
                'is_loopback': mic.isloopback,
                'channels': mic.channels,
            }
            sources.append(source_info)
            logger.debug(f"发现音频源: {mic.name} (loopback={mic.isloopback})")

        return sources

    def set_audio_source(self, source):
        """
        设置音频源（结构化或旧格式裸字符串）

        Args:
            source: ``{'kind':'device','id'}`` | ``{'kind':'process','pid','name'}``；
                旧格式设备 id 裸字符串继续被接受（``''``=默认回环设备）。
                None 视为非法。

        Raises:
            ValueError: 目标非法；失败时当前源保持不变。
        """
        if source is None:
            raise ValueError('未提供音频源')
        if isinstance(source, str):
            source = {'kind': 'device', 'id': source}
        if not isinstance(source, dict):
            raise ValueError(f'非法的音频源格式: {source!r}')

        kind = source.get('kind', 'device')
        if kind == 'device':
            self._set_device_source(source.get('id') or '')
        elif kind == 'process':
            self._set_process_source(source)
        else:
            raise ValueError(f'未知的音频源类型: {kind!r}')

    def _set_device_source(self, device_id: str):
        """切换到设备源；``device_id=''`` 表示默认回环设备。先校验、后落状态。"""
        if device_id == '':
            self._reset_to_device('')
            logger.info("已设置音频源: 默认回环设备（整个系统）")
            return

        microphones = sc.all_microphones(include_loopback=True)
        for mic in microphones:
            if mic.id == device_id:
                self._reset_to_device(device_id)
                self._microphone = mic
                logger.info(f"已设置音频源: {mic.name}")
                return
        raise ValueError(f"未找到音频源: {device_id}")

    def _set_process_source(self, source: dict):
        """切换到进程源；校验门控 + PID 存活 + 名称匹配后落状态（含 create_time）。"""
        pid = source.get('pid')
        name = source.get('name')
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError(f'非法的进程 PID: {pid!r}')
        if not isinstance(name, str) or not name:
            raise ValueError('进程源缺少名称')
        if not process_loopback_supported():
            raise ValueError(
                '当前系统不支持按应用捕获（需 Windows 10 2004 / Build 19041+）'
            )

        try:
            proc = psutil.Process(pid)
            actual_name = proc.name()
            create_time = proc.create_time()
        except psutil.NoSuchProcess:
            raise ValueError(f'进程不存在: {name} (pid={pid})')
        except Exception as e:  # noqa: BLE001 - 权限/僵尸进程等一律视为不可用
            raise ValueError(f'无法访问进程 {name} (pid={pid}): {e}')

        if actual_name.lower() != name.lower():
            raise ValueError(
                f'进程名称不匹配: 期望 {name}，实际 {actual_name} (pid={pid})'
            )

        self._source_kind = 'process'
        self._device_id = ''
        self._microphone = None
        self._process_pid = int(pid)
        self._process_name = name
        self._process_create_time = float(create_time)
        logger.info(f"已设置音频源: 进程 {name} (pid={pid})")

    def _reset_to_device(self, device_id: str):
        """清空进程源状态并回到设备源（device_id='' 为默认设备）。"""
        self._source_kind = 'device'
        self._device_id = device_id or ''
        self._microphone = None
        self._process_pid = None
        self._process_name = None
        self._process_create_time = None

    def set_source_lost_callback(self, callback: Optional[Callable[[str, int], None]]):
        """注入进程源退出回调（由 watchdog 线程触发，回调方负责切回事件循环）。"""
        self._on_source_lost = callback

    def get_current_source(self) -> dict:
        """返回当前运行源的协议形状（供 get_config 回传 / 前端对齐）。"""
        if self._source_kind == 'process' and self._process_pid is not None:
            return {
                'kind': 'process',
                'name': self._process_name,
                'pid': self._process_pid,
            }
        return {'kind': 'device', 'id': self._device_id or ''}

    def _resample_audio(self, audio: np.ndarray, original_rate: int) -> np.ndarray:
        """
        重采样音频到目标采样率（soxr 多相滤波）

        Args:
            audio: 原始音频数据
            original_rate: 原始采样率

        Returns:
            重采样后的 float32 音频数据
        """
        if original_rate == self.sample_rate:
            return audio.astype(np.float32)

        resampled = soxr.resample(audio, original_rate, self.sample_rate, quality='HQ')
        return np.asarray(resampled, dtype=np.float32)

    def _convert_to_mono(self, audio: np.ndarray) -> np.ndarray:
        """
        将音频转换为单声道

        Args:
            audio: 多声道音频数据

        Returns:
            单声道音频数据
        """
        if audio.ndim == 1:
            return audio

        # 多声道取平均
        return np.mean(audio, axis=1)

    def _process_audio(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        """
        处理音频数据：转单声道、重采样、归一化

        Args:
            audio: 原始音频数据
            sample_rate: 原始采样率

        Returns:
            处理后的 float32 单声道音频
        """
        # 转单声道
        audio = self._convert_to_mono(audio)

        # 归一化到 [-1, 1] 的 float32
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype == np.int32:
            audio = audio.astype(np.float32) / 2147483648.0
        elif audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        # 重采样
        audio = self._resample_audio(audio, sample_rate)

        return audio

    async def start(self, callback: AudioCallback):
        """
        开始音频捕获（按当前源类型分派：设备回环 / 按进程回环）

        Args:
            callback: 音频数据回调（同步函数，在录音线程中直接调用，
                      应只做环形缓冲写入等轻量操作）
        """
        if self._running:
            logger.warning("音频捕获已在运行")
            return

        self._callback = callback
        self._running = True
        self._paused = False

        if self._source_kind == 'process':
            await self._start_process_capture()
        else:
            await self._start_device_capture()

    def _pick_default_loopback(self, loopback_mics):
        """选择"整个系统"默认回环设备。

        优先取与 ``sc.default_speaker().name`` 同名的回环设备（即系统默认
        扬声器端点）；名称取不到/无匹配时回退首个回环设备（既有行为）。
        """
        try:
            default_name = sc.default_speaker().name
        except Exception:  # noqa: BLE001 - 无默认设备/枚举异常时回退
            default_name = None
        if default_name:
            for mic in loopback_mics:
                if mic.name == default_name:
                    return mic
        return loopback_mics[0]

    async def _start_device_capture(self):
        """设备回环捕获（原逻辑）。"""
        # 如果没有设置音频源，使用默认回环设备
        if self._microphone is None:
            microphones = sc.all_microphones(include_loopback=True)
            loopback_mics = [m for m in microphones if m.isloopback]
            if loopback_mics:
                self._microphone = self._pick_default_loopback(loopback_mics)
                logger.info(f"使用默认回环设备: {self._microphone.name}")
            else:
                self._running = False
                raise RuntimeError("未找到回环音频设备")

        # 创建录音器
        self._recorder = self._microphone.recorder(
            samplerate=self.sample_rate,
            channels=self.channels,
            blocksize=self.chunk_size
        )

        logger.info("开始音频捕获（设备回环）")

        # 录音循环放后台线程，start 立即返回（不得阻塞启动序列）
        self._record_task = asyncio.create_task(asyncio.to_thread(self._record_loop))

    async def _start_process_capture(self):
        """进程回环捕获；启动失败回退默认设备源（不冒泡，保证启动/切换不崩）。"""
        pid = self._process_pid
        name = self._process_name

        if not process_loopback_supported():
            logger.warning("当前系统不支持按进程捕获，回退默认设备源")
            self._reset_to_device('')
            await self._start_device_capture()
            return

        capture = ProcessLoopbackCapture(pid, self._on_process_audio)
        try:
            # ProcessLoopbackCapture.start 同步阻塞至激活完成 → 移出事件循环
            await asyncio.to_thread(capture.start)
        except Exception as e:  # noqa: BLE001 - 门控/激活失败均回退
            logger.error(
                f"进程回环捕获启动失败（{name} pid={pid}），回退默认设备源: {e}"
            )
            self._reset_to_device('')
            await self._start_device_capture()
            return

        self._process_capture = capture
        self._arm_watchdog()
        logger.info(f"开始音频捕获（进程 {name} pid={pid}）")

    def _on_process_audio(self, audio: np.ndarray):
        """进程回环回调（捕获线程）：复用 _process_audio（单声道 + soxr→16k）。"""
        if self._paused:
            return
        try:
            processed = self._process_audio(audio, PROCESS_SAMPLE_RATE)
        except Exception as e:
            logger.error(f"进程音频处理错误: {e}")
            return
        if self._callback:
            try:
                self._callback(processed)
            except Exception as e:
                logger.error(f"音频回调错误: {e}")

    def _record_loop(self):
        """录音循环（在独立线程中运行）"""
        try:
            with self._recorder:
                while self._running:
                    # 录制音频块（暂停时也继续读取，防止 WASAPI 缓冲区溢出）
                    audio = self._recorder.record(numframes=self.chunk_size)

                    # 暂停期间直接丢弃
                    if self._paused:
                        continue

                    if audio is not None and len(audio) > 0:
                        # 处理音频
                        processed_audio = self._process_audio(audio, self.sample_rate)

                        # 同步回调（只写环形缓冲）
                        if self._callback:
                            try:
                                self._callback(processed_audio)
                            except Exception as e:
                                logger.error(f"音频回调错误: {e}")
        except Exception as e:
            logger.error(f"录音循环错误: {e}")
            self._running = False

    async def stop(self):
        """停止音频捕获（等待录音线程退出；含进程流与 watchdog）"""
        self._running = False
        self._disarm_watchdog()

        if self._record_task:
            try:
                await asyncio.wait_for(self._record_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._record_task = None

        capture = self._process_capture
        self._process_capture = None
        if capture is not None:
            try:
                # ProcessLoopbackCapture.stop 同步 join（≤2s）→ 移出事件循环
                await asyncio.to_thread(capture.stop)
            except Exception as e:  # noqa: BLE001 - 停止失败不阻断
                logger.error(f"停止进程回环捕获失败: {e}")

        logger.info("音频捕获已停止")

    async def restart(self):
        """重启捕获（切换音频源后调用）；未运行时不动作"""
        if not self._running:
            return
        callback = self._callback
        was_paused = self._paused
        await self.stop()
        await self.start(callback)
        if was_paused:
            self.pause()

    # ------------------------------------------------------------------ #
    # 进程源 watchdog：PID 存活 + create_time 复核（防 PID 复用）
    # ------------------------------------------------------------------ #

    def _arm_watchdog(self):
        """武装进程源 watchdog（暂停期间保持存活）。"""
        self._disarm_watchdog()
        if self._source_kind != 'process' or self._process_pid is None:
            return

        stop_event = threading.Event()
        self._watchdog_stop = stop_event
        pid = self._process_pid
        name = self._process_name
        expected_create_time = self._process_create_time
        interval = self._watchdog_interval_s

        def _watch():
            while not stop_event.wait(interval):
                lost = False
                try:
                    proc = psutil.Process(pid)
                    create_time = proc.create_time()
                    if (
                        expected_create_time is None
                        or abs(create_time - expected_create_time) > _CREATE_TIME_TOLERANCE
                    ):
                        lost = True
                except psutil.NoSuchProcess:
                    lost = True
                except Exception:  # noqa: BLE001 - 瞬时异常不误判
                    logger.debug("watchdog 检测异常（忽略本次）", exc_info=True)

                if lost:
                    logger.info(f"进程音频源已退出: {name} (pid={pid})")
                    self._notify_source_lost(name, pid)
                    return

        thread = threading.Thread(
            target=_watch, name=f'process-watchdog-{pid}', daemon=True
        )
        self._watchdog_thread = thread
        thread.start()

    def _disarm_watchdog(self):
        """解除 watchdog（不阻塞：置事件后由线程自行退出）。"""
        stop_event = self._watchdog_stop
        self._watchdog_stop = None
        self._watchdog_thread = None
        if stop_event is not None:
            stop_event.set()

    def _notify_source_lost(self, name: Optional[str], pid: Optional[int]):
        """在 watchdog 线程触发注入回调；异常不得冒泡。"""
        callback = self._on_source_lost
        if callback is None:
            return
        try:
            callback(name, pid)
        except Exception:  # noqa: BLE001
            logger.exception("音频源退出回调异常")

    def pause(self):
        """暂停音频捕获（继续读流但丢弃）"""
        self._paused = True
        logger.info("音频捕获已暂停")

    def resume(self):
        """恢复音频捕获（从实况音频开始，不补播暂停期内容）"""
        self._paused = False
        logger.info("音频捕获已恢复")

    @property
    def is_paused(self) -> bool:
        return self._paused
