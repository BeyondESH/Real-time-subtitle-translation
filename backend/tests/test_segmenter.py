"""
VAD 语句切分器与环形缓冲测试

切句逻辑用注入的 VAD stub 驱动（确定性）；
Silero 对合成音调的检出本身不稳定，不作为断言依据。
"""
import numpy as np
import pytest

from audio_buffer import RingBuffer, UtteranceSegmenter

SR = 16000


def sine(seconds: float, freq: float = 440.0) -> np.ndarray:
    t = np.arange(int(SR * seconds), dtype=np.float32) / SR
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(SR * seconds), dtype=np.float32)


class StubVad:
    """按预设队列返回 VAD 结果（位置相对传入窗口）"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, window: np.ndarray):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return []


class TestRingBuffer:
    def test_append_and_read(self):
        buf = RingBuffer(SR)  # 1 秒容量
        buf.append(sine(0.5))
        assert buf.total_written == SR // 2
        out = buf.read_range(0, SR // 2)
        assert len(out) == SR // 2

    def test_wraparound(self):
        buf = RingBuffer(SR)
        buf.append(sine(0.8))
        buf.append(sine(0.6))  # 总量 1.4s，超过容量
        assert buf.total_written == int(SR * 1.4)
        assert buf.oldest_available == int(SR * 0.4)
        # 越界读取被裁剪到最旧可用
        out = buf.read_range(0, int(SR * 1.4))
        assert len(out) == SR

    def test_oversized_append_keeps_tail(self):
        buf = RingBuffer(SR)
        buf.append(sine(2.0))  # 一次写入超过容量
        assert buf.total_written == 2 * SR
        out = buf.read_range(0, 2 * SR)
        assert len(out) == SR


class TestUtteranceSegmenter:
    def test_silence_speech_silence_cut(self):
        """静音/语音/静音：句尾静音达阈值后切出，含头卷"""
        buf = RingBuffer(30 * SR)
        vad = StubVad([
            # 两次 tick 窗口均从 0 开始（第一次未消费）：语音段 4800..52800
            [{'start': 4800, 'end': 52800}],
            [{'start': 4800, 'end': 52800}],
        ])
        seg = UtteranceSegmenter(sample_rate=SR, vad_fn=vad, head_pad_ms=300)

        buf.append(silence(0.3))
        buf.append(sine(3.0))
        buf.append(silence(0.25))  # 尾部静音 250ms < 600ms
        assert seg.tick(buf) == []  # 语音仍在生长，不切

        buf.append(silence(1.0))   # 尾部静音累计 1.25s ≥ 600ms
        utterances = seg.tick(buf)
        assert len(utterances) == 1
        # 切出 = 头卷 0.3s + 语音 3.0s
        assert len(utterances[0].audio) == int(0.3 * SR) + int(3.0 * SR)
        # 时间戳：seg_start=4800 头卷后钳到 0，ts_end=52800/16000=3.3
        assert utterances[0].ts_start == 0.0
        assert utterances[0].ts_end == pytest.approx(3.3)
        # 消费位推进到句尾
        assert seg.consumed == 4800 + 48000
        # 检出语音段后 speech_active 为 True
        assert seg.speech_active is True

    def test_force_cut_on_max_length(self):
        """超长语句在 max_utterance_s 处强制切分"""
        buf = RingBuffer(60 * SR)
        vad = StubVad([
            # 20s 连续语音，尾部无静音 → 强制切
            [{'start': 0, 'end': 20 * SR}],
            # 剩余 5s 语音 + 尾部静音充足 → 正常切出
            [{'start': 0, 'end': 5 * SR}],
        ])
        seg = UtteranceSegmenter(sample_rate=SR, vad_fn=vad, max_utterance_s=15)

        buf.append(sine(20.0))
        utterances = seg.tick(buf)
        assert len(utterances) == 1
        assert len(utterances[0].audio) == 15 * SR
        assert seg.consumed == 15 * SR
        assert utterances[0].ts_start == 0.0
        assert utterances[0].ts_end == pytest.approx(15.0)

        buf.append(silence(1.0))
        utterances = seg.tick(buf)
        assert len(utterances) == 1
        assert len(utterances[0].audio) == 5 * SR + seg.head_pad_samples
        # 第二段：base=240000，seg 0..80000 → ts_start=(240000-4800)/16000=14.7，ts_end=20.0
        assert utterances[0].ts_start == pytest.approx(14.7)
        assert utterances[0].ts_end == pytest.approx(20.0)

    def test_pure_silence_no_emit(self):
        """纯静音：不切句，消费位推进但保留头卷"""
        buf = RingBuffer(30 * SR)
        vad = StubVad([[], []])
        seg = UtteranceSegmenter(sample_rate=SR, vad_fn=vad, head_pad_ms=300)

        buf.append(silence(2.0))
        assert seg.tick(buf) == []
        assert seg.consumed == 2 * SR - seg.head_pad_samples
        assert seg.speech_active is False

    def test_multiple_segments_one_tick(self):
        """一次 tick 切出多条完整语句"""
        buf = RingBuffer(30 * SR)
        vad = StubVad([
            [
                {'start': 1600, 'end': 17600},    # 句1
                {'start': 25600, 'end': 41600},   # 句2（尾部静音充足）
            ],
        ])
        seg = UtteranceSegmenter(sample_rate=SR, vad_fn=vad)

        buf.append(silence(0.1))
        buf.append(sine(1.0))
        buf.append(silence(0.5))
        buf.append(sine(1.0))
        buf.append(silence(1.0))
        utterances = seg.tick(buf)
        assert len(utterances) == 2
        assert seg.consumed == 41600

    def test_buffer_overflow_resync(self):
        """缓冲区溢出时消费位跳齐到最旧可用"""
        buf = RingBuffer(SR)
        vad = StubVad([[]])
        seg = UtteranceSegmenter(sample_rate=SR, vad_fn=vad)

        buf.append(sine(3.0))  # 溢出（容量 1s）
        seg.tick(buf)
        assert seg.consumed >= buf.oldest_available

    def test_real_vad_runs_without_crash(self):
        """真实 Silero VAD 冒烟：可运行、纯静音检出为空（不断言语音检出）"""
        faster_whisper = pytest.importorskip('faster_whisper')
        buf = RingBuffer(30 * SR)
        seg = UtteranceSegmenter(sample_rate=SR)  # 不注入 stub，走真实 VAD
        buf.append(silence(1.0))
        assert seg.tick(buf) == []

    def test_speech_active_unchanged_on_insufficient_data(self):
        """数据量不足未执行 VAD 的 tick 不改变 speech_active（早退路径）"""
        buf = RingBuffer(30 * SR)
        vad = StubVad([[{'start': 0, 'end': int(15.02 * SR)}]])
        seg = UtteranceSegmenter(sample_rate=SR, vad_fn=vad, max_utterance_s=15)

        # 超长强制切分：consumed 推进到 15s，窗口尾部只剩 320 样本
        buf.append(sine(15.02))
        utterances = seg.tick(buf)
        assert len(utterances) == 1
        assert seg.speech_active is True
        assert vad.calls == 1

        # 新增仅 160 样本（未读总量 480 < 100ms 阈值）：早退，不触发 VAD，状态保持
        buf.append(silence(0.01))
        assert seg.tick(buf) == []
        assert seg.speech_active is True
        assert vad.calls == 1

        # 足量纯静音窗口 → 状态翻 False
        vad._responses.append([])
        buf.append(silence(1.0))
        seg.tick(buf)
        assert seg.speech_active is False
        assert vad.calls == 2
