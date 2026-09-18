"""
真实语音端到端验证（静音 TTS 音源，不触碰声卡、零可听输出）

用法:
    python scripts/verify_pipeline_real_audio.py <zh.wav> <en.wav>
    （WAV 要求: 16kHz / 单声道 / 16bit，由 System.Speech SetOutputToWaveFile 生成）

证明范围（与 PyInstaller 冻结产物同源的代码路径）:
  1. 真实 Silero VAD 对真实语音切句（既有 pytest 仅覆盖合成音调/stub/静音）
  2. 真实 faster-whisper(tiny/cpu/int8) ASR 识别内容与源语言判定
  3. UtteranceSegment.ts_start/ts_end 时间戳在真实链路上的正确性（P2 增量）
  4. 同语言跳过语义（source=target 时零推理、不加载翻译模型）
  5. VadStateBroadcaster 在真实语音上的翻转序列（speech → silence）

不在范围（如实声明）: MT 真推理——翻译模型（Hy-MT2-1.8B GGUF，约 1.1GB）不随包，
本脚本不擅自下载；且日语专用翻译模型无对应 TTS 语音，该环节由真机验收
（播放日/英语内容）覆盖。
"""
import asyncio
import sys
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio_buffer import RingBuffer, UtteranceSegmenter  # noqa: E402
from asr_engine import ASREngine  # noqa: E402
from translator import Translator  # noqa: E402
from vad_events import VadStateBroadcaster  # noqa: E402

SR = 16000
CHUNK = 1024

_results = []
_exit = 0


def check(name: str, ok: bool, extra: str = '') -> None:
    global _exit
    _results.append(f"{'PASS' if ok else 'FAIL'} | {name}" + (f" ({extra})" if extra else ''))
    if not ok:
        _exit = 1


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, 'rb') as w:
        assert w.getframerate() == SR, f'{path}: 需要 16kHz'
        assert w.getnchannels() == 1, f'{path}: 需要单声道'
        assert w.getsampwidth() == 2, f'{path}: 需要 16bit'
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def make_config(targets):
    return {
        'audio': {'sample_rate': SR, 'channels': 1, 'chunk_size': CHUNK},
        'asr': {'model_size': 'tiny', 'device': 'cpu', 'compute_type': 'int8', 'language': None},
        'translation': {
            'default_model': 'hy-mt2-1.8b-q4km',
            'download': {'source': 'auto'},
            'target_languages': targets,
            'device': 'cpu',
            'n_ctx': 4096,
            'timeout_s': 30,
        },
    }


async def run_scenario(name: str, wav_path: str, targets, expect_lang: str, keywords):
    t_start = time.time()
    audio = load_wav(wav_path)
    config = make_config(targets)
    buf = RingBuffer(30 * SR)
    seg = UtteranceSegmenter(sample_rate=SR)
    broadcaster = VadStateBroadcaster()
    asr = ASREngine(config)
    translator = Translator(config)

    await asr.initialize()
    await translator.initialize()

    # ---- 以 1024 样本块喂入（模拟捕获回调），每块 tick 一次 + VAD 状态广播 ----
    # 时钟用音频虚拟时间（total_written/SR）：生产环境 tick 随真实音频节奏推进，
    # 而驱动毫秒级喂完全部音频，若用墙钟则 300ms 去抖窗口永远走不满（v1 的假 FAIL）
    utterances = []
    vad_events = []
    speech_seen = False

    def feed(pcm: np.ndarray):
        nonlocal speech_seen
        for i in range(0, len(pcm), CHUNK):
            buf.append(pcm[i:i + CHUNK])
            utterances.extend(seg.tick(buf))
            if seg.speech_active:
                speech_seen = True
            vclock = buf.total_written / SR
            change = broadcaster.on_tick(seg.speech_active, vclock)
            if change:
                vad_events.append(change)

    feed(audio)
    feed(np.zeros(SR, dtype=np.float32))  # 1s 尾静音触发句尾判定

    check(f'[{name}] 真实 Silero VAD 切出语句', len(utterances) >= 1,
          f'{len(utterances)} 条')
    check(f'[{name}] 语音期间 speech_active 置位', speech_seen)
    check(f'[{name}] VadStateBroadcaster 真实翻转 speech→silence',
          'speech' in vad_events and vad_events[-1] == 'silence',
          f'events={vad_events}')

    if not utterances:
        return

    # ---- 时间戳（P2 增量）----
    ts_ok = all(
        u.ts_start is not None and u.ts_end is not None and u.ts_end > u.ts_start
        for u in utterances
    )
    u0 = utterances[0]
    check(f'[{name}] UtteranceSegment 时间戳有效', ts_ok,
          f'first=[{u0.ts_start:.2f}s, {u0.ts_end:.2f}s]')

    # ---- 真实 ASR + 同语言跳过 ----
    payloads = []
    for u in utterances:
        t = await asr.transcribe(u.audio)
        if not t or not t.get('text'):
            continue
        active = targets[0]
        translations = await translator.translate(t['text'], t['language'], [active])
        payloads.append({
            'type': 'subtitle',
            'original': t['text'],
            'source_language': t['language'],
            'active_language': active,
            'translations': translations,
            'ts_start': round(u.ts_start, 3),
            'ts_end': round(u.ts_end, 3),
        })

    check(f'[{name}] ASR 产出非空识别结果', len(payloads) >= 1, f'{len(payloads)} 条')
    if not payloads:
        return

    joined = ' '.join(p['original'] for p in payloads).lower()
    check(f'[{name}] 识别内容命中预期关键词', any(k.lower() in joined for k in keywords),
          f'text={joined[:60]}')
    langs = {p['source_language'] for p in payloads}
    check(f'[{name}] 源语言判定为 {expect_lang}', langs == {expect_lang}, f'langs={langs}')

    # 同语言跳过：source==target → translations 无该语言条目且未触发模型下载/加载
    skip_ok = all(p['active_language'] not in (p['translations'] or {}) for p in payloads)
    check(f'[{name}] 同语言跳过（零翻译推理）', skip_ok,
          f'translations={[p["translations"] for p in payloads]}')

    shape_ok = all(
        set(p) == {'type', 'original', 'source_language', 'active_language',
                   'translations', 'ts_start', 'ts_end'}
        for p in payloads
    )
    check(f'[{name}] subtitle 载荷形状完整（含 ts 字段）', shape_ok)
    _results.append(f'INFO | [{name}] 耗时 {time.time() - t_start:.1f}s')


async def main(zh_wav: str, en_wav: str):
    await run_scenario('zh', zh_wav, ['zh'], 'zh',
                       ['天气', '天氣', '会议', '會議', '三点', '三點', '下午'])
    await run_scenario('en', en_wav, ['en'], 'en',
                       ['weather', 'meeting', 'afternoon', 'three', 'nice'])


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if len(sys.argv) != 3:
        print('用法: python scripts/verify_pipeline_real_audio.py <zh.wav> <en.wav>')
        sys.exit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
    print('\n'.join(_results))
    print(f'REAL_AUDIO_VERIFY_DONE exitCode={_exit}')
    sys.exit(_exit)
