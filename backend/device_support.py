"""
推理设备探测与静默降级决策。

设计要点（replace-asr-engine-with-funasr-nano design.md D5）：
- ASR 经 sherpa-onnx（内嵌 onnxruntime）执行，以 onnxruntime 暴露的 CUDA
  provider 可用性探测（注：sherpa-onnx wheel 自带 ORT，与独立 onnxruntime 包
  可能不同构建；探针为尽力判定，实际以加载路径的端到端热身为准，
  失败按 load_failed/runtime_failed 静默降级）；
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


def normalize_device(value) -> str:
    """将任意配置值归一为合法偏好，非法值按 'auto' 处理。"""
    return value if value in VALID_DEVICES else 'auto'


def _sherpa_version_tag() -> str:
    """sherpa-onnx 版本标识（模块级便于测试注入；导入失败返回空串）"""
    try:
        import sherpa_onnx

        return getattr(sherpa_onnx, '__version__', '') or ''
    except Exception:  # noqa: BLE001 - 探针异常降为回落路径
        return ''


def _probe_sherpa_cuda_variant() -> Optional[dict]:
    """
    sherpa-onnx wheel 自身的 CUDA 能力探测（CUDA 变体 wheel 自带 CUDA 版
    onnxruntime 与 cuDNN9 DLL，与独立 `onnxruntime` 包是两套构建）。

    CUDA 变体的版本本地版本含 `+cuda` 标识（如 `1.13.8+cuda12.cudnn9`）——
    这是 ASR 实际运行时暴露的最直接能力信号；CPU 变体返回 None（回落独立
    onnxruntime 探针）。
    """
    version = _sherpa_version_tag()
    if '+cuda' in version:
        return {
            'cuda_available': True,
            'source': 'sherpa_onnx',
            'detail': f'sherpa-onnx {version}（CUDA 变体，自带 CUDA 版 onnxruntime）',
        }
    return None


def _probe_onnxruntime() -> dict:
    """
    ASR 探针：优先以 sherpa-onnx wheel 的 CUDA 能力为准（ASR 实际经其内嵌 ORT
    推理）；sherpa-onnx 为 CPU 变体时回落独立 onnxruntime 包的 provider 列表。

    注意：独立 `onnxruntime` 包与 sherpa-onnx 自带的 ORT 可能是不同构建——探针
    为尽力判定，实际以加载路径的端到端热身为准（失败按 load_failed/runtime_failed
    静默降级）。
    """
    sherpa_probe = _probe_sherpa_cuda_variant()
    if sherpa_probe is not None:
        return sherpa_probe
    try:
        import onnxruntime

        providers = onnxruntime.get_available_providers()
        available = 'CUDAExecutionProvider' in providers
        return {
            'cuda_available': available,
            'source': 'onnxruntime',
            'detail': f'providers={providers}',
        }
    except Exception as e:  # noqa: BLE001 - 探针异常一律降为不可用，不让异常逃逸
        return {
            'cuda_available': False,
            'source': 'none',
            'detail': f'onnxruntime 探针不可用: {e}',
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
        'asr': _probe_onnxruntime(),
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
