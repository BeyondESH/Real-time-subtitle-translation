"""model_downloader 单元测试：流式下载 / 断点续传 / 校验失败 / 源切换（MockTransport）"""
import hashlib

import httpx
import pytest

from model_downloader import DownloadError, download_model
from translation_models import TranslationModel

CHUNK = 64 * 1024
PAYLOAD = bytes(range(256)) * 1200 + b'tail'  # ~300KB 确定性数据
SHA = hashlib.sha256(PAYLOAD).hexdigest()


def make_model(**overrides) -> TranslationModel:
    base = dict(
        id='test-model',
        display_name='Test Model',
        repo='org/repo',
        revision='a' * 40,
        filename='test.gguf',
        size_bytes=len(PAYLOAD),
        sha256=SHA,
        license='Apache-2.0',
        license_note='',
        profile='hy-mt2',
    )
    base.update(overrides)
    return TranslationModel(**base)


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )


class TestFreshDownload:
    async def test_download_and_progress(self, tmp_path):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=PAYLOAD)

        reports = []
        client = make_client(handler)
        try:
            path = await download_model(
                make_model(), progress=lambda n, p, m: reports.append((n, p, m)),
                cache_root=tmp_path, client=client,
            )
        finally:
            await client.aclose()

        assert path.read_bytes() == PAYLOAD
        assert not path.with_name(path.name + '.part').exists()
        assert reports[0][0] == 'Test Model'
        assert any(p == 100 for _, p, _ in reports)

    async def test_already_downloaded_skips(self, tmp_path):
        model = make_model()
        final = tmp_path / model.id / model.filename
        final.parent.mkdir(parents=True)
        final.write_bytes(PAYLOAD)
        calls = {'n': 0}

        def handler(request):
            calls['n'] += 1
            return httpx.Response(200, content=b'')

        client = make_client(handler)
        try:
            path = await download_model(make_model(), cache_root=tmp_path, client=client)
        finally:
            await client.aclose()
        assert path == final and calls['n'] == 0


class TestResume:
    async def test_range_resume(self, tmp_path):
        model = make_model()
        part = tmp_path / model.id / (model.filename + '.part')
        part.parent.mkdir(parents=True)
        cut = 100_000
        part.write_bytes(PAYLOAD[:cut])  # 预置半成品（.part）
        seen_range = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_range['value'] = request.headers.get('range')
            assert request.headers.get('range') == f'bytes={cut}-'
            return httpx.Response(
                206,
                content=PAYLOAD[cut:],
                headers={'Content-Range': f'bytes {cut}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}'},
            )

        client = make_client(handler)
        try:
            path = await download_model(model, cache_root=tmp_path, client=client)
        finally:
            await client.aclose()

        assert seen_range['value'] == f'bytes={cut}-'
        assert path.read_bytes() == PAYLOAD

    async def test_server_ignores_range_restarts(self, tmp_path):
        model = make_model()
        part = tmp_path / model.id / (model.filename + '.part')
        part.parent.mkdir(parents=True)
        part.write_bytes(PAYLOAD[:50_000])

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers.get('range') == 'bytes=50000-'
            return httpx.Response(200, content=PAYLOAD)  # 忽略 Range

        client = make_client(handler)
        try:
            path = await download_model(model, cache_root=tmp_path, client=client)
        finally:
            await client.aclose()
        assert path.read_bytes() == PAYLOAD


class TestChecksum:
    async def test_mismatch_discards_and_errors(self, tmp_path):
        bad = bytearray(PAYLOAD)
        bad[-1] ^= 0xFF  # 同长度、不同内容

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=bytes(bad))

        client = make_client(handler)
        try:
            with pytest.raises(DownloadError, match='所有下载源均失败'):
                await download_model(make_model(), cache_root=tmp_path, client=client)
        finally:
            await client.aclose()

        assert not (tmp_path / 'test-model' / 'test.gguf').exists()
        assert not (tmp_path / 'test-model' / 'test.gguf.part').exists()


class TestSourceFallback:
    async def test_official_fails_mirror_succeeds(self, tmp_path):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.host)
            if request.url.host == 'huggingface.co':
                return httpx.Response(500)
            return httpx.Response(200, content=PAYLOAD)

        client = make_client(handler)
        try:
            path = await download_model(make_model(), cache_root=tmp_path, client=client)
        finally:
            await client.aclose()

        assert path.read_bytes() == PAYLOAD
        assert calls[0] == 'huggingface.co' and 'hf-mirror.com' in calls

    async def test_no_sources_raises(self, tmp_path):
        client = make_client(lambda request: httpx.Response(200, content=b''))
        try:
            with pytest.raises(DownloadError, match='没有可用的下载源'):
                await download_model(
                    make_model(), source='modelscope', cache_root=tmp_path, client=client
                )
        finally:
            await client.aclose()
