# -*- coding: utf-8 -*-
"""真音频解码计时：ja.wav 在 GPU/CPU 两个 provider 下的解码耗时对比。

用法：python -X utf8 scripts/bench_ja_wav.py
"""
import io
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from asr_engine import ensure_bundled_cuda_dll_path  # noqa: E402

ensure_bundled_cuda_dll_path()

import asr_models  # noqa: E402
import numpy as np  # noqa: E402
import sherpa_onnx  # noqa: E402

WAV = Path(r'C:\Users\LIJIAC~1\AppData\Local\Temp\codemaker\ja.wav')


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), 'rb') as f:
        assert f.getnchannels() == 1, f.getnchannels()
        sr = f.getframerate()
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    return sr, data.astype(np.float32) / 32768.0


def build(provider: str):
    paths = asr_models.asr_model_paths()
    return sherpa_onnx.OfflineRecognizer.from_funasr_nano(
        encoder_adaptor=str(paths['encoder_adaptor']),
        llm=str(paths['llm']),
        embedding=str(paths['embedding']),
        tokenizer=str(paths['tokenizer']),
        provider=provider, num_threads=2, language='日文', itn=True, hotwords='',
    )


def run(provider: str, sr: int, samples: np.ndarray):
    t0 = time.monotonic()
    recognizer = build(provider)
    t1 = time.monotonic()
    stream = recognizer.create_stream()
    stream.accept_waveform(sr, samples)
    recognizer.decode_stream(stream)
    t2 = time.monotonic()
    # 二次解码（探热后速度）
    stream2 = recognizer.create_stream()
    stream2.accept_waveform(sr, samples)
    recognizer.decode_stream(stream2)
    t3 = time.monotonic()
    dur = len(samples) / sr
    print(f'[{provider}] audio {dur:.2f}s | construct {t1 - t0:.1f}s | '
          f'decode#1 {t2 - t1:.1f}s (RTF {(t2 - t1) / dur:.3f}) | '
          f'decode#2 {t3 - t2:.1f}s (RTF {(t3 - t2) / dur:.3f})', flush=True)
    print(f'[{provider}] text = {stream.result.text!r}', flush=True)


sr, samples = read_wav(WAV)
print(f'wav: {WAV.name} sr={sr} dur={len(samples) / sr:.2f}s', flush=True)
run('cuda', sr, samples)
run('cpu', sr, samples)
