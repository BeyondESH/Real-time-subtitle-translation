"""
设备探测与静默降级决策纯函数测试（monkeypatch ct2/llama 全组合）

不加载真实模型、不访问网络。
"""
import sys
import types

import pytest

from device_support import (
    DEVICE_REASONS,
    VALID_DEVICES,
    decide_device,
    normalize_device,
    probe_compute,
)


def fake_ct2(count=None, raises=None):
    module = types.SimpleNamespace()
    if raises is not None:
        def _boom():
            raise raises
        module.get_cuda_device_count = _boom
    else:
        module.get_cuda_device_count = lambda: count
    return module


@pytest.fixture
def llama_probe(monkeypatch, tmp_path):
    """
    替身化 llama.cpp 翻译探针的底层输入：
    - devices: `--list-devices` 的返回行
    - raises: list_devices 抛出的异常
    - missing: 二进制缺失场景
    """
    def _setup(devices=None, raises=None, missing=False):
        exe = tmp_path / ('missing.exe' if missing else 'llama-server.exe')
        if not missing:
            exe.write_bytes(b'')
        monkeypatch.setattr(
            'device_support.resolve_device_binary', lambda root, device: exe
        )
        if raises is not None:
            def _boom(_exe):
                raise raises
            monkeypatch.setattr('device_support.list_devices', _boom)
        else:
            monkeypatch.setattr(
                'device_support.list_devices', lambda _exe: list(devices or [])
            )
    return _setup


class TestProbeCompute:
    def test_both_available(self, monkeypatch, llama_probe):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(1))
        llama_probe(devices=['CUDA0: NVIDIA GeForce RTX 5060 (8123 MiB, 7031 MiB free)'])
        result = probe_compute()
        assert result['asr']['cuda_available'] is True
        assert result['asr']['source'] == 'ctranslate2'
        assert result['translation']['cuda_available'] is True
        assert result['translation']['source'] == 'llama_cpp'

    def test_asr_only(self, monkeypatch, llama_probe):
        """CTranslate2 可用而 llama.cpp 枚举不到 CUDA 设备（或仅为 CPU 构建）"""
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(1))
        llama_probe(devices=[])
        result = probe_compute()
        assert result['asr']['cuda_available'] is True
        assert result['translation']['cuda_available'] is False

    def test_translation_only(self, monkeypatch, llama_probe):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(0))
        llama_probe(devices=['CUDA0: NVIDIA GeForce RTX 5060 (8123 MiB, 7031 MiB free)'])
        result = probe_compute()
        assert result['asr']['cuda_available'] is False
        assert result['translation']['cuda_available'] is True

    def test_both_unavailable(self, monkeypatch, llama_probe):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(0))
        llama_probe(devices=['(none)'])
        result = probe_compute()
        assert result['asr']['cuda_available'] is False
        assert result['translation']['cuda_available'] is False

    def test_probe_exceptions_do_not_escape(self, monkeypatch, llama_probe):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(raises=RuntimeError('ct2 炸了')))
        llama_probe(raises=RuntimeError('llama 炸了'))
        result = probe_compute()  # MUST NOT raise
        assert result['asr']['cuda_available'] is False
        assert result['asr']['source'] == 'none'
        assert 'ct2 炸了' in result['asr']['detail']
        assert result['translation']['cuda_available'] is False
        assert result['translation']['source'] == 'none'
        assert 'llama 炸了' in result['translation']['detail']

    def test_probe_import_error_do_not_escape(self, monkeypatch, llama_probe):
        # None in sys.modules → import 抛 ImportError，应被吞掉
        monkeypatch.setitem(sys.modules, 'ctranslate2', None)
        llama_probe(missing=True)
        result = probe_compute()
        assert result['asr']['cuda_available'] is False
        assert result['translation']['cuda_available'] is False
        assert '缺失' in result['translation']['detail']

    def test_binary_missing_degrades(self, llama_probe):
        llama_probe(missing=True)
        result = probe_compute()
        assert result['translation']['cuda_available'] is False
        assert result['translation']['source'] == 'llama_cpp'
        assert '缺失' in result['translation']['detail']

    def test_probes_independent(self, monkeypatch, llama_probe):
        """一个探针异常不影响另一个的判定"""
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(raises=RuntimeError('x')))
        llama_probe(devices=['CUDA0: NVIDIA GeForce RTX 5060 (8123 MiB, 7031 MiB free)'])
        result = probe_compute()
        assert result['asr']['cuda_available'] is False
        assert result['translation']['cuda_available'] is True


class TestDecideDevice:
    def test_auto_with_cuda(self):
        d = decide_device('auto', {'cuda_available': True})
        assert d == {'resolved': 'cuda', 'reason': 'auto', 'attempt_cuda': True}

    def test_user_cuda_with_cuda(self):
        d = decide_device('cuda', {'cuda_available': True})
        assert d == {'resolved': 'cuda', 'reason': 'user', 'attempt_cuda': True}

    def test_auto_without_cuda(self):
        d = decide_device('auto', {'cuda_available': False})
        assert d == {'resolved': 'cpu', 'reason': 'no_cuda', 'attempt_cuda': False}

    def test_explicit_cuda_without_probe(self):
        """显式 cuda 但探针不可用：不发起注定失败的 GPU 加载"""
        d = decide_device('cuda', {'cuda_available': False})
        assert d == {'resolved': 'cpu', 'reason': 'no_cuda', 'attempt_cuda': False}

    def test_explicit_cpu(self):
        d = decide_device('cpu', {'cuda_available': True})
        assert d == {'resolved': 'cpu', 'reason': 'user', 'attempt_cuda': False}

    def test_gpu_load_failed_degrades(self):
        for pref in ('auto', 'cuda'):
            d = decide_device(pref, {'cuda_available': True}, load_result=False)
            assert d == {'resolved': 'cpu', 'reason': 'load_failed', 'attempt_cuda': False}

    def test_gpu_load_success(self):
        d = decide_device('auto', {'cuda_available': True}, load_result=True)
        assert d == {'resolved': 'cuda', 'reason': 'auto', 'attempt_cuda': True}

    def test_invalid_preference_raises(self):
        with pytest.raises(ValueError):
            decide_device('tpu', {'cuda_available': True})

    def test_reasons_enum_is_exact(self):
        assert DEVICE_REASONS == ('auto', 'user', 'no_cuda', 'load_failed', 'runtime_failed')


class TestNormalizeDevice:
    def test_valid_passthrough(self):
        for value in VALID_DEVICES:
            assert normalize_device(value) == value

    def test_invalid_falls_back_auto(self):
        assert normalize_device('tpu') == 'auto'
        assert normalize_device(None) == 'auto'
