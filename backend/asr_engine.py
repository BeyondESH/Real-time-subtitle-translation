"""
ASR 引擎模块 - 基于 faster-whisper 的语音识别
"""
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# 模型下载进度回调类型
DownloadProgressCallback = Callable[[str, float, str], None]


class ASREngine:
    """faster-whisper ASR 引擎"""

    # 支持的模型大小
    SUPPORTED_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3']

    # 模型大小映射 (模型名 -> 大小描述)
    MODEL_SIZES = {
        'tiny': '39MB',
        'base': '74MB',
        'small': '244MB',
        'medium': '769MB',
        'large-v3': '1.5GB'
    }

    def __init__(self, config: dict):
        self.config = config.get('asr', {})
        self.model_size = self.config.get('model_size', 'base')
        self.device = self.config.get('device', 'auto')
        self.language = self.config.get('language', None)
        self.compute_type = self.config.get('compute_type', 'float16')

        # 模型缓存目录
        self.model_cache_dir = Path.home() / '.cache' / 'subtitle-translator' / 'whisper'
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)

        self._model: Optional[WhisperModel] = None
        self._initialized = False
        self._switching = False
        self._switch_lock: Optional[asyncio.Lock] = None  # 延迟创建（需事件循环）
        self._download_progress_callback: Optional[DownloadProgressCallback] = None

    @property
    def is_ready(self) -> bool:
        """模型已初始化且不在切换中"""
        return self._initialized and not self._switching

    def _detect_device(self) -> str:
        """
        检测可用的计算设备

        Returns:
            设备类型: 'cuda' 或 'cpu'
        """
        if self.device == 'auto':
            try:
                import torch
                if torch.cuda.is_available():
                    logger.info("检测到 CUDA GPU，使用 GPU 加速")
                    return 'cuda'
                else:
                    logger.info("未检测到 CUDA GPU，使用 CPU")
                    return 'cpu'
            except ImportError:
                logger.warning("PyTorch 未安装，使用 CPU")
                return 'cpu'
        return self.device

    def set_download_progress_callback(self, callback: DownloadProgressCallback):
        """
        设置下载进度回调函数

        Args:
            callback: 回调函数，参数为 (model_name, progress_percent, status_message)
        """
        self._download_progress_callback = callback

    def _report_progress(self, model_name: str, progress: float, message: str):
        """报告下载进度"""
        if self._download_progress_callback:
            self._download_progress_callback(model_name, progress, message)
        logger.info(f"[{model_name}] {progress:.1f}% - {message}")

    async def initialize(self):
        """初始化 ASR 引擎"""
        if self._initialized:
            return

        logger.info(f"正在加载 Whisper 模型: {self.model_size}")
        self._report_progress(self.model_size, 0, "准备加载模型...")
        start_time = time.time()

        device = self._detect_device()

        # 根据设备选择计算类型
        if device == 'cpu':
            compute_type = 'int8'  # CPU 使用 INT8 加速
        else:
            compute_type = self.compute_type

        self._report_progress(self.model_size, 10, f"正在下载模型 ({self.MODEL_SIZES.get(self.model_size, '未知')})...")

        # 在线程中加载模型（避免阻塞）
        # faster-whisper 会自动下载模型到缓存目录
        self._model = await asyncio.to_thread(
            self._load_model_with_progress,
            self.model_size,
            device,
            compute_type
        )

        elapsed = time.time() - start_time
        self._report_progress(self.model_size, 100, f"模型加载完成 ({elapsed:.1f}s)")
        logger.info(f"模型加载完成，耗时: {elapsed:.2f}s，设备: {device}")
        self._initialized = True

    def _load_model_with_progress(self, model_size: str, device: str, compute_type: str):
        """
        加载模型（同步，在线程中运行）

        Args:
            model_size: 模型大小
            device: 计算设备
            compute_type: 计算类型

        Returns:
            加载的模型
        """
        try:
            # faster-whisper 会自动处理模型下载
            # 模型会缓存在 ~/.cache/huggingface/ 或指定目录
            model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type,
                download_root=str(self.model_cache_dir)
            )
            return model

        except Exception as e:
            logger.error(f"加载模型失败: {e}")
            raise

    async def transcribe(self, audio_data: np.ndarray) -> Optional[dict]:
        """
        识别音频数据

        Args:
            audio_data: 音频数据 (float32, 16kHz, mono)

        Returns:
            识别结果，包含 text 和 language 字段
        """
        if not self._initialized or self._model is None:
            logger.error("ASR 引擎未初始化")
            return None

        if len(audio_data) == 0:
            return None

        try:
            start_time = time.time()

            # 在线程中执行识别
            segments, info = await asyncio.to_thread(
                self._transcribe_sync,
                audio_data
            )

            # 合并所有片段
            text = ''.join([segment.text for segment in segments]).strip()

            elapsed = time.time() - start_time
            logger.debug(f"识别完成，耗时: {elapsed:.3f}s，语言: {info.language}")

            if not text:
                return None

            return {
                'text': text,
                'language': info.language,
                'confidence': info.language_probability
            }

        except Exception as e:
            logger.error(f"识别错误: {e}")
            return None

    def _transcribe_sync(self, audio_data: np.ndarray):
        """
        同步识别（在线程中运行）

        Args:
            audio_data: 音频数据

        Returns:
            (segments, info) 元组
        """
        # 确保音频数据格式正确
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32)

        # 如果是单声道但形状不对，调整一下
        if audio_data.ndim > 1:
            audio_data = audio_data.flatten()

        segments, info = self._model.transcribe(
            audio_data,
            language=self.language,
            beam_size=5,
            vad_filter=True,  # 启用 VAD 过滤静音
            vad_parameters=dict(
                min_silence_duration_ms=500,
                speech_pad_ms=200
            )
        )

        return list(segments), info

    async def change_model(self, model_size: str):
        """
        切换 Whisper 模型（锁串行化，切换期间 is_ready=False，语句被丢弃）

        Args:
            model_size: 新的模型大小

        Raises:
            ValueError: 模型名不受支持
        """
        if model_size not in self.SUPPORTED_MODELS:
            raise ValueError(f"不支持的模型: {model_size}，支持: {self.SUPPORTED_MODELS}")

        if model_size == self.model_size and self._initialized:
            logger.info(f"模型 {model_size} 已加载")
            return

        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()

        async with self._switch_lock:
            self._switching = True
            try:
                logger.info(f"切换模型: {self.model_size} -> {model_size}")
                self.model_size = model_size
                self._initialized = False
                self._model = None  # 释放旧模型

                await self.initialize()
            finally:
                self._switching = False

    def get_model_info(self) -> dict:
        """
        获取当前模型信息

        Returns:
            模型信息字典
        """
        return {
            'model_size': self.model_size,
            'device': self.device,
            'compute_type': self.compute_type,
            'initialized': self._initialized,
            'supported_models': self.SUPPORTED_MODELS
        }
