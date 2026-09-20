"""
ASR 模型注册表与下载编排测试（httpx MockTransport，无网络/无大文件）

覆盖 replace-asr-engine-with-funasr-nano tasks 2.1/2.3：
- 全量下载（多文件、大小+sha256 校验、进度聚合、幂等短路）
- 多源切换（官方失败 → 镜像）、sha256 不符切换源、双源失败 DownloadError
- 非 LFS 文件（无 sha256）仅按大小校验
- 引擎集成：模型缺失 → 先下载（进度可见）→ 加载 → 就绪
"""
import hashlib
import types

import httpx
import pytest

import asr_engine as asr_mod
import asr_models as am
from asr_engine import ASREngine
from asr_models import (
    ASR_MODEL_FILES,
    DownloadError,
    ensure_asr_model_downloaded,
    is_asr_model_downloaded,
)

# 测试用迷你资产清单（真实清单 948MB 不可在单测中下载）
_TINY_FILES = (
    am.AsrModelFile(
        rel_path='encoder.int8.onnx',
        size_bytes=10,
        sha256=hashlib.sha256(b'0123456789').hexdigest(),
    ),
    am.AsrModelFile(
        rel_path='Qwen3-0.6B/tok.json',
        size_bytes=5,
        sha256=hashlib.sha256(b'token').hexdigest(),
    ),
    am.AsrModelFile(
        rel_path='Qwen3-0.6B/merges.txt',
        size_bytes=3,
        sha256=None,  # 非 LFS：仅大小校验
    ),
)
_TINY_TOTAL = sum(f.size_bytes for f in _TINY_FILES)


@pytest.fixture
def tiny_registry(monkeypatch):
    monkeypatch.setattr(am, 'ASR_MODEL_FILES', _TINY_FILES)
    monkeypatch.setattr(am, '_ASR_TOTAL_BYTES', _TINY_TOTAL)
    return _TINY_FILES


def _content_for(rel_path: str) -> bytes:
    return {
        'encoder.int8.onnx': b'0123456789',
        'Qwen3-0.6B/tok.json': b'token',
        'Qwen3-0.6B/merges.txt': b'mrg',
    }[rel_path]


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestEnsureModelDownloaded:
    async def test_downloads_all_files_with_progress(self, tiny_registry, tmp_path):
        """全量下载：逐文件落地、校验通过、进度聚合、完成后幂等短路"""
        served = []

        def handler(request: httpx.Request) -> httpx.Response:
            served.append(str(request.url))
            rel = str(request.url).rsplit('/', 1)[-1]
            if 'Qwen3-0.6B' in str(request.url):
                name = 'Qwen3-0.6B/tok.json' if 'tok' in rel else 'Qwen3-0.6B/merges.txt'
            else:
                name = 'encoder.int8.onnx'
            return httpx.Response(200, content=_content_for(name))

        client = make_client(handler)
        progress = []
        root = await ensure_asr_model_downloaded(
            progress=lambda n, p, m: progress.append((n, p, m)),
            cache_root=tmp_path, client=client
        )
        await client.aclose()

        assert root == am.asr_cache_root(tmp_path)
        assert is_asr_model_downloaded(tmp_path)
        for f in _TINY_FILES:
            assert (root / f.rel_path).stat().st_size == f.size_bytes
        # 进度：至少一条开始与完成；聚合进度单调不减、终值 100
        percents = [p[1] for p in progress]
        assert percents[-1] == 100.0
        assert percents == sorted(percents)
        # 幂等：再次调用不再发起请求（本地短路）
        served.clear()
        client2 = make_client(handler)
        await ensure_asr_model_downloaded(cache_root=tmp_path, client=client2)
        await client2.aclose()
        assert served == []

    async def test_official_failure_switches_to_mirror(self, tiny_registry, tmp_path):
        """官方源 500 → 镜像源成功（逐文件多源切换）"""
        urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            if 'huggingface.co' in str(request.url):
                return httpx.Response(500)
            rel = str(request.url)
            if 'tok.json' in rel:
                return httpx.Response(200, content=b'token')
            if 'merges.txt' in rel:
                return httpx.Response(200, content=b'mrg')
            return httpx.Response(200, content=b'0123456789')

        client = make_client(handler)
        await ensure_asr_model_downloaded(cache_root=tmp_path, client=client)
        await client.aclose()

        assert is_asr_model_downloaded(tmp_path)
        assert any('huggingface.co' in u for u in urls)
        assert any('hf-mirror.com' in u for u in urls)

    async def test_sha256_mismatch_tries_next_source(self, tiny_registry, tmp_path):
        """sha256 校验失败 → 丢弃并切换下一个源（最终以正确内容落地）"""
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if 'encoder.int8.onnx' in url:
                if 'huggingface.co' in url:
                    return httpx.Response(200, content=b'9999999999')  # 同长错误内容
                return httpx.Response(200, content=b'0123456789')
            if 'tok.json' in url:
                return httpx.Response(200, content=b'token')
            return httpx.Response(200, content=b'mrg')

        client = make_client(handler)
        await ensure_asr_model_downloaded(cache_root=tmp_path, client=client)
        await client.aclose()

        p = am.asr_cache_root(tmp_path) / 'encoder.int8.onnx'
        assert p.read_bytes() == b'0123456789'

    async def test_all_sources_fail_raises(self, tiny_registry, tmp_path):
        """全部源失败 → DownloadError"""
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = make_client(handler)
        with pytest.raises(DownloadError):
            await ensure_asr_model_downloaded(cache_root=tmp_path, client=client)
        await client.aclose()

    async def test_size_only_file_verified_by_size(self, tiny_registry, tmp_path):
        """无 sha256 的文件仅按大小校验（内容可校验到落盘即通过）"""
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if 'tok.json' in url:
                return httpx.Response(200, content=b'token')
            if 'merges.txt' in url:
                return httpx.Response(200, content=b'mrg')
            return httpx.Response(200, content=b'0123456789')

        client = make_client(handler)
        await ensure_asr_model_downloaded(cache_root=tmp_path, client=client)
        await client.aclose()
        assert is_asr_model_downloaded(tmp_path)


class TestEngineModelDownloadIntegration:
    async def test_initialize_downloads_missing_model_first(self, monkeypatch, tmp_path):
        """首装无模型 → 先下载（进度可见）→ 加载 → 就绪（tasks 2.3）"""
        state = {'downloaded': False}
        calls = []

        async def fake_ensure(progress=None, source='auto', cache_root=None, client=None):
            calls.append(progress)
            state['downloaded'] = True
            if progress:
                progress('Fun-ASR-Nano', 100.0, '识别模型下载完成')

        def fake_builder(provider):
            return _FakeRecognizer(provider)

        class _FakeStream:
            def accept_waveform(self, sample_rate, samples):
                pass

            @property
            def result(self):
                return types.SimpleNamespace(text='')

        class _FakeRecognizer:
            def __init__(self, provider):
                self.provider = provider

            def create_stream(self):
                return _FakeStream()

            def decode_stream(self, stream):
                pass

        monkeypatch.setattr(asr_mod, 'is_asr_model_downloaded', lambda: state['downloaded'])
        monkeypatch.setattr(asr_mod, 'ensure_asr_model_downloaded', fake_ensure)
        monkeypatch.setattr(
            asr_mod, 'probe_compute', lambda: {'asr': {
                'cuda_available': False, 'source': 'onnxruntime', 'detail': 'test'}}
        )

        progress_reports = []
        engine = ASREngine({'asr': {'device': 'auto'}})
        engine.set_download_progress_callback(
            lambda name, pct, msg: progress_reports.append((name, pct))
        )
        monkeypatch.setattr(engine, '_build_recognizer', fake_builder)

        await engine.initialize()

        assert len(calls) == 1  # 下载在加载前执行且仅一次
        assert ('Fun-ASR-Nano', 100.0) in progress_reports  # 进度经 model_progress 形状可见
        assert engine.is_ready
        assert engine.resolved_device == 'cpu'
        assert engine.device_reason == 'no_cuda'

    def test_registry_manifest_complete(self):
        """真实资产清单完整性：路径唯一、大小为正、LFS 文件有 sha256"""
        rels = [f.rel_path for f in ASR_MODEL_FILES]
        assert len(rels) == len(set(rels))
        for f in ASR_MODEL_FILES:
            assert f.size_bytes > 0
            assert f.sha256 is None or len(f.sha256) == 64
        # 引擎构造所需四项齐备
        paths = am.asr_model_paths()
        for key in ('encoder_adaptor', 'llm', 'embedding', 'tokenizer'):
            assert key in paths
