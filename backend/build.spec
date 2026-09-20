# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置
# 用法: pyinstaller build.spec --clean
# 调试版（带控制台）: 先 set CONSOLE=1 再执行
import os
from PyInstaller.utils.hooks import collect_all

block_cipher = None
console = os.environ.get('CONSOLE', '0') == '1'

datas = [
    ('../config.yaml', '.'),
    # 内嵌 Silero VAD 资产（faster-whisper MIT 实现内嵌，MUST NOT 依赖 faster-whisper 包）
    ('vendor/silero_vad/assets/silero_vad_v6.onnx', 'vendor/silero_vad/assets'),
]
binaries = []
hiddenimports = [
    'soundcard',
    'numpy',
    'sherpa_onnx',
    'httpx',
    'websockets',
    'yaml',
    'soxr',
    # 本地模块显式收集：免受 cwd/分析路径差异影响（曾出现 Analysis 漏收
    # audio_buffer 导致打包产物 ModuleNotFoundError 的事故）
    'audio_buffer',
    'audio_capture',
    'asr_engine',
    'asr_models',
    'device_support',
    'pipeline_worker',
    'translator',
    'llama_server_manager',
    'model_downloader',
    'translation_models',
    'translation_profiles',
    'language_codes',
    'vad_events',
    'websocket_server',
    'vendor.silero_vad.vad',
]

# 原生库与数据资产完整收集（sherpa-onnx 含其依赖的 onnxruntime 运行时；
# ASR 引擎为 Fun-ASR-Nano 进程内推理，翻译经 llama.cpp 进程外运行）
for _pkg in ('sherpa_onnx', 'onnxruntime'):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(
    ['main.py'],
    pathex=[SPECPATH],  # 锚定 spec 所在目录，本地模块解析不依赖调用方 cwd
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ASR 引擎已迁移至 sherpa-onnx（Fun-ASR-Nano 进程内），翻译引擎为
        # llama.cpp 进程外（llama-server sidecar）：不再分发 torch/transformers/
        # ctranslate2/tokenizers/faster-whisper 链路。sentencepiece 为 transformers
        # 的传递可选依赖，一并排除。
        'torch', 'transformers', 'sentencepiece', 'torchvision', 'torchaudio',
        'ctranslate2', 'tokenizers', 'faster_whisper',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SubtitleTranslator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 大型科学计算 DLL 经 UPX 压缩有损坏风险，关闭
    console=console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../frontend/resources/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SubtitleTranslator',
)
