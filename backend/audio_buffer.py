"""
音频缓冲与 VAD 语句切分模块

RingBuffer：定长环形缓冲，线程安全（录音线程写入，事件循环线程读取）。
UtteranceSegmenter：基于 faster-whisper 内置 Silero VAD 的语句切分，
仅在事件循环线程周期性调用 tick()，非线程安全。
"""
import logging
import threading
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# VAD 函数类型：输入 float32 单声道音频，返回 [{'start': int, 'end': int}, ...]
VadFn = Callable[[np.ndarray], List[dict]]


@dataclass
class UtteranceSegment:
    """
    切句产出：语句音频 + 切句器时间线上的起止时间戳（秒）。

    ts_start/ts_end 以样本位置换算（sample_pos / sample_rate），相对进程
    捕获时钟单调递增，供客户端导出精确 SRT 时间轴；兼容路径（裸数组注入）
    下可为 None，客户端以接收时刻近似。
    """
    audio: np.ndarray
    ts_start: Optional[float] = None
    ts_end: Optional[float] = None


class RingBuffer:
    """定长环形音频缓冲（float32 单声道，线程安全）"""

    def __init__(self, capacity_samples: int):
        if capacity_samples <= 0:
            raise ValueError(f"容量必须为正: {capacity_samples}")
        self._buf = np.zeros(capacity_samples, dtype=np.float32)
        self._capacity = capacity_samples
        self._write_pos = 0  # 绝对写入位置（单调递增）
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def total_written(self) -> int:
        """已写入样本总数（绝对位置）"""
        with self._lock:
            return self._write_pos

    @property
    def oldest_available(self) -> int:
        """缓冲区中最早仍可读样本的绝对位置"""
        with self._lock:
            return max(0, self._write_pos - self._capacity)

    def append(self, data: np.ndarray):
        """追加音频数据。长于容量的输入只保留尾部。"""
        if len(data) == 0:
            return
        if data.dtype != np.float32:
            data = data.astype(np.float32)
        with self._lock:
            n = len(data)
            if n >= self._capacity:
                self._buf[:] = data[-self._capacity:]
                self._write_pos += n
                return
            start = self._write_pos % self._capacity
            end = start + n
            if end <= self._capacity:
                self._buf[start:end] = data
            else:
                first = self._capacity - start
                self._buf[start:] = data[:first]
                self._buf[:end - self._capacity] = data[first:]
            self._write_pos += n

    def read_range(self, start_abs: int, end_abs: int) -> np.ndarray:
        """读取绝对区间 [start_abs, end_abs)，越界部分裁剪到可用范围。"""
        with self._lock:
            oldest = max(0, self._write_pos - self._capacity)
            start = max(start_abs, oldest)
            end = min(end_abs, self._write_pos)
            if end <= start:
                return np.zeros(0, dtype=np.float32)
            indices = np.arange(start, end) % self._capacity
            return self._buf[indices].copy()


class UtteranceSegmenter:
    """
    基于 VAD 的语句切分器。

    每个 tick 对缓冲区中未消费的音频做一次 VAD 分析：
    - 句尾静音达到 min_silence_ms → 语句完成，切出
    - 语句长度达到 max_utterance_s → 强制切分
    - 切出的语句带 head_pad_ms 头部预卷
    - 纯静音窗口直接消费（保留 head_pad 供下一句起音）
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence_ms: int = 600,
        speech_pad_ms: int = 200,
        max_utterance_s: float = 15.0,
        head_pad_ms: int = 300,
        vad_fn: Optional[VadFn] = None,
    ):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.min_silence_ms = min_silence_ms
        self.speech_pad_ms = speech_pad_ms
        self.min_silence_samples = int(sample_rate * min_silence_ms / 1000)
        self.max_utterance_samples = int(sample_rate * max_utterance_s)
        self.head_pad_samples = int(sample_rate * head_pad_ms / 1000)
        self._min_emit_samples = int(sample_rate * 0.1)  # 短于 100ms 不产出
        self._vad_fn = vad_fn  # None → 首次 tick 时加载 faster-whisper 内置 VAD
        self._consumed = 0  # 已消费的绝对样本位置
        self._speech_active = False  # 最近一次 tick 的 VAD 语音检出状态

    @property
    def consumed(self) -> int:
        return self._consumed

    @property
    def speech_active(self) -> bool:
        """最近一次执行了 VAD 分析的 tick 是否检出语音段（供 vad_state 翻转广播）。

        数据量不足未执行 VAD 的 tick 不改变本状态。
        """
        return self._speech_active

    def _default_vad(self, audio: np.ndarray) -> List[dict]:
        """faster-whisper 内置 Silero VAD（模型为进程内单例）"""
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        options = VadOptions(
            threshold=self.threshold,
            min_silence_duration_ms=self.min_silence_ms,
            speech_pad_ms=self.speech_pad_ms,
        )
        return get_speech_timestamps(
            audio, vad_options=options, sampling_rate=self.sample_rate
        )

    def tick(self, buffer: RingBuffer) -> List[UtteranceSegment]:
        """
        对未消费音频做一次分析，返回切出的完整语句列表。

        Args:
            buffer: 音频环形缓冲

        Returns:
            切出的语句段（UtteranceSegment：音频 + 时间戳）列表，可能为空
        """
        end = buffer.total_written

        # 缓冲溢出兜底：消费位落后于最早可用位置时跳齐
        oldest = buffer.oldest_available
        if self._consumed < oldest:
            logger.warning(f"缓冲区溢出，跳过 {oldest - self._consumed} 个样本")
            self._consumed = oldest

        if end - self._consumed < self._min_emit_samples:
            return []

        base = self._consumed
        window = buffer.read_range(base, end)
        if len(window) == 0:
            return []

        vad_fn = self._vad_fn or self._default_vad
        segments = vad_fn(window)
        self._speech_active = bool(segments)

        if not segments:
            # 纯静音：消费掉，仅保留 head_pad 作为下一句的起音上下文
            self._consumed = max(self._consumed, end - self.head_pad_samples)
            return []

        utterances: List[UtteranceSegment] = []
        for i, seg in enumerate(segments):
            seg_start = base + seg['start']
            seg_end = base + seg['end']
            is_last = i == len(segments) - 1

            if is_last:
                trailing_silence = end - seg_end
                complete = trailing_silence >= self.min_silence_samples
                too_long = (seg_end - seg_start) >= self.max_utterance_samples
                if not complete and not too_long:
                    break  # 最后一段仍在生长，留待下次 tick
                if too_long and not complete:
                    # 超长强制切分，剩余部分下轮重新检测
                    seg_end = seg_start + self.max_utterance_samples

            audio_start = max(seg_start - self.head_pad_samples, 0)
            audio = buffer.read_range(audio_start, seg_end)
            if len(audio) >= self._min_emit_samples:
                utterances.append(UtteranceSegment(
                    audio=audio,
                    ts_start=audio_start / self.sample_rate,
                    ts_end=seg_end / self.sample_rate,
                ))
            self._consumed = max(self._consumed, seg_end)

        return utterances
