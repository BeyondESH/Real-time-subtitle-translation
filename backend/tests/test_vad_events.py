"""
vad_state 翻转广播单测（pipeline-control spec：VAD 状态广播）

场景：翻转事件 / 短间隙去抖不抖动 / 同状态不重复 / 初始静音单次广播。
无客户端安全性由 WebSocketServer.send（无客户端即返回）保证，不在此测。
"""
import pytest

from vad_events import VadStateBroadcaster


class TestVadStateBroadcaster:
    def test_speech_immediate_and_no_repeat(self):
        """speech 起始立即广播；同状态后续 tick 不重复"""
        b = VadStateBroadcaster()
        assert b.on_tick(True, 0.0) == 'speech'
        assert b.on_tick(True, 0.25) is None
        assert b.on_tick(True, 0.5) is None
        assert b.current == 'speech'

    def test_silence_debounced_300ms(self):
        """silence 起始去抖：持续静音 ≥300ms 才广播，且仅一次"""
        b = VadStateBroadcaster()
        assert b.on_tick(True, 0.0) == 'speech'
        assert b.on_tick(False, 1.0) is None    # 静音起点，仅记录时刻
        assert b.on_tick(False, 1.2) is None    # 200ms < 300ms
        assert b.on_tick(False, 1.35) == 'silence'  # ≥300ms 触发
        assert b.on_tick(False, 2.0) is None    # 不再重复
        assert b.current == 'silence'

    def test_short_gap_no_flip(self):
        """200ms 短停顿被去抖吞并：不产生 silence，聆听指示连续"""
        b = VadStateBroadcaster()
        assert b.on_tick(True, 0.0) == 'speech'
        assert b.on_tick(False, 0.5) is None    # 间隙开始
        assert b.on_tick(True, 0.7) is None     # 200ms 后恢复语音，无翻转事件
        assert b.current == 'speech'

    def test_initial_silence_single_broadcast(self):
        """启动即静音：去抖后广播一次 silence，之后静默"""
        b = VadStateBroadcaster()
        assert b.current is None
        assert b.on_tick(False, 0.0) is None
        assert b.on_tick(False, 0.4) == 'silence'
        assert b.on_tick(False, 0.8) is None

    def test_reentry_cycle(self):
        """speech→silence→speech 完整循环各自只广播一次"""
        b = VadStateBroadcaster()
        assert b.on_tick(True, 0.0) == 'speech'
        b.on_tick(False, 1.0)
        assert b.on_tick(False, 1.4) == 'silence'
        assert b.on_tick(True, 2.0) == 'speech'
        b.on_tick(False, 3.0)
        assert b.on_tick(False, 3.4) == 'silence'

    def test_negative_debounce_rejected(self):
        with pytest.raises(ValueError):
            VadStateBroadcaster(silence_debounce_s=-0.1)
