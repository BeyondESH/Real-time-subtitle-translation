# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置
# 用法: pyinstaller build.spec --clean
# 调试版（带控制台）: 先 set CONSOLE=1 再执行
import os
from PyInstaller.utils.hooks import collect_all

block_cipher = None
console = os.environ.get('CONSOLE', '0') == '1'

datas = [('../config.yaml', '.')]
binaries = []
hiddenimports = [
    'soundcard',
    'numpy',
    'faster_whisper',
    'httpx',
    'websockets',
    'yaml',
    'soxr',
    # 按进程音频捕获依赖（pycaw.__init__ 为空，需显式收集 pycaw.pycaw 以带入
    # pycaw.api.* 子模块；comtypes.client 触发 hooks-contrib 的 comtypes 收集）
    'comtypes',
    'comtypes.client',
    'pycaw',
    'pycaw.pycaw',
    'psutil',
    # 本地模块显式收集：免受 cwd/分析路径差异影响（曾出现 Analysis 漏收
    # audio_buffer 导致打包产物 ModuleNotFoundError 的事故）
    'process_loopback',
    'audio_buffer',
    'audio_capture',
    'asr_engine',
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
]

# 原生库与数据资产完整收集（含 faster-whisper 的 silero_vad.onnx）
for _pkg in ('ctranslate2', 'tokenizers', 'faster_whisper', 'onnxruntime'):
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
        # 翻译引擎已迁移至 llama.cpp（llama-server sidecar）：不再分发 torch/transformers。
        # collect_all('ctranslate2') 的 converters 子模块会【可选导入】torch/transformers
        # （第 27/28 行 try: import），必须显式排除，否则整包被拖入（实测 +2.8GB）。
        # sentencepiece 为 transformers 的传递可选依赖，一并排除（faster-whisper 链路不需要）。
        'torch', 'transformers', 'sentencepiece', 'torchvision', 'torchaudio',
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
