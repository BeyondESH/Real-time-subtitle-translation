"""
推理设备探测与静默降级决策。

设计要点（design.md D5 / 原 design.md D7）：
- ASR 经 faster-whisper → CTranslate2 执行，必须以 CTranslate2 的 CUDA 设备数探测；
- 翻译经 llama.cpp（随包 llama-server 子进程）执行，必须以该构建的
  `--list-devices` 设备枚举探测（cuda 构建能枚举到 CUDA 设备才算可用）；
- 两引擎独立判定，任一探针导入/调用异常一律降为不可用，异常 MUST NOT 逃逸；
- 降级决策为纯函数（偏好 × 探针 × 加载结果 → 实际设备 + reason），便于单测。
"""
from pathlib import Path  # noqa: F401 - 类型提示与测试替身使用
from typing import Optional

from llama_server_manager import list_devices, resolve_device_binary

# 合法设备偏好（store / 控制协议 / config.yaml 同一词汇）
VALID_DEVICES = ('auto', 'cpu', 'cuda')

# 设备选择/降级原因枚举（design.md D8；runtime_failed=加载成功后运行期推理不可用）
DEVICE_REASONS = ('auto', 'user', 'no_cuda', 'load_failed', 'runtime_failed')

# 偏好为 cpu 或探针不可用时的 CPU 计算类型
CPU_COMPUTE_TYPE = 'int8'


def normalize_device(value) -> str:
    """将任意配置值归一为合法偏好，非法值按 'auto' 处理。"""
    return value if value in VALID_DEVICES else 'auto'


def _probe_ctranslate2() -> dict:
    """ASR 探针：CTranslate2 可见的 CUDA 设备数（异常降为不可用）。"""
    try:
        import ctranslate2
        count = ctranslate2.get_cuda_device_count()
        return {
            'cuda_available': bool(count and count > 0),
            'source': 'ctranslate2',
            'detail': f'get_cuda_device_count()={count}',
        }
    except Exception as e:  # noqa: BLE001 - 探针异常一律降为不可用，不让异常逃逸
        return {
            'cuda_available': False,
            'source': 'none',
            'detail': f'ctranslate2 探针不可用: {e}',
        }


def _probe_llama() -> dict:
    """翻译探针：llama.cpp CUDA 构建的设备枚举（二进制缺失/异常/无设备降为不可用）。"""
    try:
        exe = resolve_device_binary(None, 'cuda')
        if not exe.exists():
            return {
                'cuda_available': False,
                'source': 'llama_cpp',
                'detail': f'llama-server CUDA 构建缺失: {exe}',
            }
        devices = list_devices(exe)
        cuda = [d for d in devices if d.upper().startswith('CUDA')]
        return {
            'cuda_available': bool(cuda),
            'source': 'llama_cpp',
            'detail': f'llama-server --list-devices: {"; ".join(devices) if devices else "(none)"}',
        }
    except Exception as e:  # noqa: BLE001 - 探针异常一律降为不可用，不让异常逃逸
        return {
            'cuda_available': False,
            'source': 'none',
            'detail': f'llama.cpp 探针不可用: {e}',
        }


def probe_compute() -> dict:
    """
    独立探测两引擎的 CUDA 可用性。

    Returns:
        {'asr': {cuda_available, source, detail},
         'translation': {cuda_available, source, detail}}
    """
    return {
        'asr': _probe_ctranslate2(),
        'translation': _probe_llama(),
    }


def decide_device(preference: str, probe: dict, load_result: Optional[bool] = None) -> dict:
    """
    降级决策纯函数：偏好 × 探针 × 加载结果 → 实际设备 + reason。

    Args:
        preference: 'auto' | 'cpu' | 'cuda'
        probe: 对应引擎探针结果（需含 'cuda_available'）
        load_result: None=尚未尝试；False=GPU 加载失败（触发 load_failed 降级）；
                     True=GPU 加载成功

    Returns:
        {'resolved': 'cuda' | 'cpu',
         'reason': 'auto' | 'user' | 'no_cuda' | 'load_failed',
         'attempt_cuda': bool}

    Raises:
        ValueError: preference 非法
    """
    if preference not in VALID_DEVICES:
        raise ValueError(f"不支持的设备偏好: {preference}，支持: {VALID_DEVICES}")

    if preference == 'cpu':
        return {'resolved': 'cpu', 'reason': 'user', 'attempt_cuda': False}

    if not bool(probe.get('cuda_available')):
        return {'resolved': 'cpu', 'reason': 'no_cuda', 'attempt_cuda': False}

    if load_result is False:
        return {'resolved': 'cpu', 'reason': 'load_failed', 'attempt_cuda': False}

    return {
        'resolved': 'cuda',
        'reason': 'user' if preference == 'cuda' else 'auto',
        'attempt_cuda': True,
    }
