"""
音频捕获模块 - WASAPI 回环设备捕获

soundcard 枚举回环设备并按选定设备（``''``=默认回环设备）开流；
音频经 ``_process_audio`` 单声道化 + soxr 重采样至 16kHz。

回调为同步函数，直接在录音线程中被调用（只写环形缓冲，不做重活）。
暂停期间继续读取音频流但直接丢弃，避免 WASAPI 缓冲区溢出。
"""
import asyncio
import logging
from typing import Callable, List, Optional

import numpy as np
import soundcard as sc
import soxr

logger = logging.getLogger(__name__)

# 音频数据回调类型：同步函数，参数为 float32 单声道 16kHz 音频
AudioCallback = Callable[[np.ndarray], None]


class AudioCapture:
    """WASAPI 音频捕获类"""

    def __init__(self, config: dict):
        self.config = config.get('audio', {})
        self.sample_rate = self.config.get('sample_rate', 16000)
        self.channels = self.config.get('channels', 1)
        self.chunk_size = self.config.get('chunk_size', 512)

        self._microphone: Optional[sc.Microphone] = None
        self._recorder: Optional[sc.Recorder] = None
        self._callback: Optional[AudioCallback] = None
        self._running = False
        self._paused = False
        self._record_task: Optional[asyncio.Task] = None

        # 音频源状态：设备回环（`_device_id=''` = 默认回环设备）
        self._device_id: str = ''

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
        设置音频源（设备-only 结构化；兼容旧格式裸字符串）

        Args:
            source: ``{'kind':'device','id'}``（``id=''``=默认回环设备）；
                旧格式设备 id 裸字符串继续被接受。其余形态
                （如 ``kind:'process'``）一律视为非法；None 视为非法。

        Raises:
            ValueError: 目标非法；失败时当前源保持不变。
        """
        if source is None:
            raise ValueError('未提供音频源')
        if isinstance(source, str):
            source = {'kind': 'device', 'id': source}
        if not isinstance(source, dict) or source.get('kind', 'device') != 'device':
            raise ValueError(f'非法的音频源格式: {source!r}')

        self._set_device_source(source.get('id') or '')

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

    def _reset_to_device(self, device_id: str):
        """回到设备源（device_id='' 为默认设备）；清空已选麦克风待 start 时重选。"""
        self._device_id = device_id or ''
        self._microphone = None

    def get_current_source(self) -> dict:
        """返回当前源的协议形状（供 get_config 回传 / 前端对齐）。"""
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
        开始音频捕获（回环设备）

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
        """停止音频捕获（等待录音线程退出）"""
        self._running = False

        if self._record_task:
            try:
                await asyncio.wait_for(self._record_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._record_task = None

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
