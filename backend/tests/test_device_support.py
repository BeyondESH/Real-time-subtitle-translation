"""
设备探测与静默降级决策纯函数测试（monkeypatch ct2/torch 全组合）

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


def fake_torch(available=None, raises=None):
    module = types.SimpleNamespace()
    if raises is not None:
        def _boom():
            raise raises
        module.cuda = types.SimpleNamespace(is_available=_boom)
    else:
        module.cuda = types.SimpleNamespace(is_available=lambda: available)
    return module


class TestProbeCompute:
    def test_both_available(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(1))
        monkeypatch.setitem(sys.modules, 'torch', fake_torch(True))
        result = probe_compute()
        assert result['asr']['cuda_available'] is True
        assert result['asr']['source'] == 'ctranslate2'
        assert result['translation']['cuda_available'] is True
        assert result['translation']['source'] == 'torch'

    def test_asr_only(self, monkeypatch):
        """CTranslate2 可用而 torch 为 CPU 构建（本变更的实证场景）"""
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(1))
        monkeypatch.setitem(sys.modules, 'torch', fake_torch(False))
        result = probe_compute()
        assert result['asr']['cuda_available'] is True
        assert result['translation']['cuda_available'] is False

    def test_translation_only(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(0))
        monkeypatch.setitem(sys.modules, 'torch', fake_torch(True))
        result = probe_compute()
        assert result['asr']['cuda_available'] is False
        assert result['translation']['cuda_available'] is True

    def test_both_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(0))
        monkeypatch.setitem(sys.modules, 'torch', fake_torch(False))
        result = probe_compute()
        assert result['asr']['cuda_available'] is False
        assert result['translation']['cuda_available'] is False

    def test_probe_exceptions_do_not_escape(self, monkeypatch):
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(raises=RuntimeError('ct2 炸了')))
        monkeypatch.setitem(sys.modules, 'torch', fake_torch(raises=RuntimeError('torch 炸了')))
        result = probe_compute()  # MUST NOT raise
        assert result['asr']['cuda_available'] is False
        assert result['asr']['source'] == 'none'
        assert 'ct2 炸了' in result['asr']['detail']
        assert result['translation']['cuda_available'] is False
        assert result['translation']['source'] == 'none'
        assert 'torch 炸了' in result['translation']['detail']

    def test_probe_import_error_do_not_escape(self, monkeypatch):
        # None in sys.modules → import 抛 ImportError，应被吞掉
        monkeypatch.setitem(sys.modules, 'ctranslate2', None)
        monkeypatch.setitem(sys.modules, 'torch', None)
        result = probe_compute()
        assert result['asr']['cuda_available'] is False
        assert result['translation']['cuda_available'] is False

    def test_probes_independent(self, monkeypatch):
        """一个探针异常不影响另一个的判定"""
        monkeypatch.setitem(sys.modules, 'ctranslate2', fake_ct2(raises=RuntimeError('x')))
        monkeypatch.setitem(sys.modules, 'torch', fake_torch(True))
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
        assert DEVICE_REASONS == ('auto', 'user', 'no_cuda', 'load_failed')


class TestNormalizeDevice:
    def test_valid_passthrough(self):
        for value in VALID_DEVICES:
            assert normalize_device(value) == value

    def test_invalid_falls_back_auto(self):
        assert normalize_device('tpu') == 'auto'
        assert normalize_device(None) == 'auto'
