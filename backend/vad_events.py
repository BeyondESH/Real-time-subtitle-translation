"""
VAD 状态翻转广播（pipeline-control spec: vad_state）

仅在状态翻转时产出事件：speech 起始立即、silence 起始去抖（默认 300ms）后，
避免语句间短停顿造成消息抖动。纯逻辑模块（不依赖 asyncio/网络），可独立单测；
无客户端时的广播跳过由 WebSocketServer.send 自身保证。
"""
from typing import Optional


class VadStateBroadcaster:
    """切句循环每 tick 喂入语音检出状态，返回需广播的翻转事件（或 None）"""

    def __init__(self, silence_debounce_s: float = 0.3):
        if silence_debounce_s < 0:
            raise ValueError(f"去抖时长不能为负: {silence_debounce_s}")
        self._debounce = silence_debounce_s
        self._broadcast: Optional[str] = None  # 'speech' | 'silence' | None（尚未广播过）
        self._silence_since: Optional[float] = None

    @property
    def current(self) -> Optional[str]:
        """最近一次已广播的状态（None=从未广播）"""
        return self._broadcast

    def on_tick(self, speech_active: bool, now: float) -> Optional[str]:
        """
        喂入一次切句状态。

        Args:
            speech_active: 本 tick 是否检出语音段
            now: 单调时钟（秒），用于静音去抖

        Returns:
            'speech' / 'silence'（需广播的翻转事件），无事件则 None
        """
        if speech_active:
            self._silence_since = None
            if self._broadcast != 'speech':
                self._broadcast = 'speech'
                return 'speech'
            return None

        # 静音侧：首次进入记录时刻，持续超过去抖窗口才翻转
        if self._silence_since is None:
            self._silence_since = now
            return None
        if self._broadcast != 'silence' and (now - self._silence_since) >= self._debounce:
            self._broadcast = 'silence'
            return 'silence'
        return None
