# -*- coding: utf-8 -*-
"""GPU 加载直连验证（dev 诊断脚本）：sherpa-onnx CUDA EP + 内嵌模型。

用法：python -X utf8 scripts/try_cuda_load.py
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 诊断输出统一 UTF-8（规避 GBK 控制台乱码）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from asr_engine import ensure_bundled_cuda_dll_path  # noqa: E402

ensure_bundled_cuda_dll_path()

import asr_models  # noqa: E402
import numpy as np  # noqa: E402
import sherpa_onnx  # noqa: E402

paths = asr_models.asr_model_paths()
print('paths ok:', {k: str(v) for k, v in paths.items()}, flush=True)
print('loading cuda recognizer (may take 30-90s)...', flush=True)

try:
    recognizer = sherpa_onnx.OfflineRecognizer.from_funasr_nano(
        encoder_adaptor=str(paths['encoder_adaptor']),
        llm=str(paths['llm']),
        embedding=str(paths['embedding']),
        tokenizer=str(paths['tokenizer']),
        provider='cuda',
        num_threads=2,
        language='日文',
        itn=True,
        hotwords='',
    )
    print('recognizer loaded: OK', flush=True)
except Exception as e:
    print('recognizer load FAILED:', type(e).__name__, str(e), flush=True)
    sys.exit(1)

stream = recognizer.create_stream()
stream.accept_waveform(16000, np.zeros(16000, dtype=np.float32))
recognizer.decode_stream(stream)
print('GPU decode OK, text =', repr(stream.result.text))
