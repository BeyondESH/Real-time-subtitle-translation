"""
音频捕获模块 - WASAPI 回环音频捕获
"""
import asyncio
import logging
from typing import Callable, List, Optional

import numpy as np
import soundcard as sc

logger = logging.getLogger(__name__)


class AudioCapture:
    """WASAPI 音频捕获类"""

    def __init__(self, config: dict):
        self.config = config.get('audio', {})
        self.sample_rate = self.config.get('sample_rate', 16000)
        self.channels = self.config.get('channels', 1)
        self.chunk_size = self.config.get('chunk_size', 1024)
        self.source_app = self.config.get('source_app', None)

        self._microphone: Optional[sc.Microphone] = None
        self._recorder: Optional[sc.Recorder] = None
        self._callback: Optional[Callable] = None
        self._running = False
        self._paused = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
                'is_loopback': mic.is_loopback,
                'channels': mic.channels,
                'sample_rate': mic.samplerate
            }
            sources.append(source_info)
            logger.debug(f"发现音频源: {mic.name} (loopback={mic.is_loopback})")

        return sources

    def set_audio_source(self, source_id: str):
        """
        设置音频源

        Args:
            source_id: 音频源 ID
        """
        microphones = sc.all_microphones(include_loopback=True)
        for mic in microphones:
            if mic.id == source_id:
                self._microphone = mic
                logger.info(f"已设置音频源: {mic.name}")
                return

        raise ValueError(f"未找到音频源: {source_id}")

    def _resample_audio(self, audio: np.ndarray, original_rate: int) -> np.ndarray:
        """
        重采样音频到目标采样率

        Args:
            audio: 原始音频数据
            original_rate: 原始采样率

        Returns:
            重采样后的音频数据
        """
        if original_rate == self.sample_rate:
            return audio

        # 简单的线性插值重采样
        duration = len(audio) / original_rate
        target_length = int(duration * self.sample_rate)

        indices = np.linspace(0, len(audio) - 1, target_length)
        resampled = np.interp(indices, np.arange(len(audio)), audio)

        return resampled.astype(np.float32)

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
        处理音频数据：重采样、转单声道

        Args:
            audio: 原始音频数据
            sample_rate: 原始采样率

        Returns:
            处理后的音频数据
        """
        # 转单声道
        audio = self._convert_to_mono(audio)

        # 重采样
        audio = self._resample_audio(audio, sample_rate)

        # 归一化到 [-1, 1]
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype == np.int32:
            audio = audio.astype(np.float32) / 2147483648.0

        return audio

    async def start(self, callback: Callable):
        """
        开始音频捕获

        Args:
            callback: 音频数据回调函数，接收 (audio_data, sample_rate) 参数
        """
        if self._running:
            logger.warning("音频捕获已在运行")
            return

        self._callback = callback
        self._running = True
        self._paused = False
        self._loop = asyncio.get_event_loop()

        # 如果没有设置音频源，使用默认回环设备
        if self._microphone is None:
            microphones = sc.all_microphones(include_loopback=True)
            loopback_mics = [m for m in microphones if m.is_loopback]
            if loopback_mics:
                self._microphone = loopback_mics[0]
                logger.info(f"使用默认回环设备: {self._microphone.name}")
            else:
                raise RuntimeError("未找到回环音频设备")

        # 创建录音器
        self._recorder = self._microphone.recorder(
            samplerate=self.sample_rate,
            channels=self.channels,
            blocksize=self.chunk_size
        )

        logger.info("开始音频捕获")

        # 在线程中运行录音
        await asyncio.to_thread(self._record_loop)

    def _record_loop(self):
        """录音循环（在独立线程中运行）"""
        try:
            with self._recorder:
                while self._running:
                    if self._paused:
                        import time
                        time.sleep(0.1)
                        continue

                    # 录制音频块
                    audio = self._recorder.record(numframes=self.chunk_size)

                    if audio is not None and len(audio) > 0:
                        # 处理音频
                        processed_audio = self._process_audio(audio, self.sample_rate)

                        # 调用回调
                        if self._callback and self._loop:
                            asyncio.run_coroutine_threadsafe(
                                self._callback(processed_audio),
                                self._loop
                            )
        except Exception as e:
            logger.error(f"录音循环错误: {e}")
            self._running = False

    async def stop(self):
        """停止音频捕获"""
        self._running = False
        logger.info("音频捕获已停止")

    def pause(self):
        """暂停音频捕获"""
        self._paused = True
        logger.info("音频捕获已暂停")

    def resume(self):
        """恢复音频捕获"""
        self._paused = False
        logger.info("音频捕获已恢复")
