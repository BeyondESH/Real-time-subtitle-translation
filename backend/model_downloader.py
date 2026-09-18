"""
翻译模型下载器：httpx 流式 + Range 断点续传 + sha256 校验 + 多源切换 + 进度回调

- 目标文件：`<cache_root>/<model-id>/<filename>`；下载中写入 `<filename>.part`
- 断点续传：`.part` 的现有字节数作为 Range 起点；服务端不支持 Range（200）时自动重下
- 多源策略：`resolve_download_sources` 的 URL 列表逐个尝试（官方 → 镜像 → ModelScope）
- 校验：完成后整体 sha256 比对；失败丢弃 `.part` 并继续下一个源
- 进度：回调形状复用 `model_progress`（name/percent/message），节流后上报

测试通过注入 `httpx.AsyncClient(MockTransport)` 完成，不访问网络。
"""
import asyncio
import hashlib
import logging
import os
from pathlib import Path
from typing import Callable, Optional

import httpx

from translation_models import (
    TranslationModel,
    model_path,
    resolve_download_sources,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, float, str], None]

_CHUNK_SIZE = 64 * 1024
_REPORT_STEP_PCT = 1.0  # 进度上报节流：每 1% 一次


class DownloadError(RuntimeError):
    """下载失败（所有源均不可用 / 校验失败）"""


def _report(callback: Optional[ProgressCallback], model_name: str,
            percent: float, message: str) -> None:
    if callback is None:
        return
    try:
        callback(model_name, percent, message)
    except Exception:  # noqa: BLE001 - 进度回调异常不得中断下载
        logger.exception('下载进度回调异常')


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


async def _download_from(
    client: httpx.AsyncClient,
    url: str,
    part: Path,
    model: TranslationModel,
    progress: Optional[ProgressCallback],
) -> None:
    """从单一源下载/续传至 .part（失败抛异常，由调用方切换源）"""
    offset = part.stat().st_size if part.exists() else 0
    if offset >= model.size_bytes:
        # 残留文件异常偏大：放弃续传，从头下载
        part.unlink(missing_ok=True)
        offset = 0

    headers = {'Range': f'bytes={offset}-'} if offset else {}
    async with client.stream('GET', url, headers=headers) as resp:
        if resp.status_code == 416:
            # Range 不可满足：重头下载
            part.unlink(missing_ok=True)
            offset, headers = 0, {}
        if offset and resp.status_code == 200:
            logger.info('下载源不支持 Range，从头下载: %s', url)
            offset = 0
        elif resp.status_code not in (200, 206):
            raise DownloadError(f'下载失败 HTTP {resp.status_code}: {url}')

        done = offset
        next_report = (int(done * 100 / model.size_bytes) // int(_REPORT_STEP_PCT) + 1) * _REPORT_STEP_PCT
        mode = 'ab' if offset else 'wb'
        with open(part, mode) as f:
            async for chunk in resp.aiter_bytes(_CHUNK_SIZE):
                f.write(chunk)
                done += len(chunk)
                pct = min(99.0, done * 100.0 / model.size_bytes)
                if pct >= next_report:
                    _report(progress, model.display_name, pct,
                            f'下载中 {pct:.0f}%（{done // (1024 * 1024)}MB / '
                            f'{model.size_bytes // (1024 * 1024)}MB）')
                    next_report = pct + _REPORT_STEP_PCT


async def download_model(
    model: TranslationModel,
    *,
    source: str = 'auto',
    progress: Optional[ProgressCallback] = None,
    cache_root: Optional[Path] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Path:
    """
    确保模型文件就绪（已就绪直接返回；否则下载 + 校验）。

    Raises:
        DownloadError: 无可用源 / 全部源失败 / sha256 校验失败
    """
    final = model_path(model, cache_root)
    if final.exists() and final.stat().st_size == model.size_bytes:
        _report(progress, model.display_name, 100, '模型已就绪')
        return final

    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + '.part')
    urls = resolve_download_sources(model, source)
    if not urls:
        raise DownloadError(f'没有可用的下载源（source={source}）')

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=15.0),
        )
    try:
        last_error: Optional[Exception] = None
        for url in urls:
            _report(progress, model.display_name, 0.0, '开始下载模型…')
            try:
                await _download_from(client, url, part, model, progress)
                digest = await asyncio.to_thread(_sha256_file, part)
                if digest != model.sha256:
                    logger.warning(
                        'sha256 校验失败（源 %s，期望 %s… 实际 %s…），丢弃并尝试其它源',
                        url, model.sha256[:12], digest[:12],
                    )
                    part.unlink(missing_ok=True)
                    last_error = DownloadError(
                        f'sha256 校验失败（实际 {digest[:12]}…）'
                    )
                    continue
                os.replace(part, final)
                _report(progress, model.display_name, 100, '模型下载完成')
                logger.info('模型下载完成: %s', final)
                return final
            except Exception as e:  # noqa: BLE001 - 单源失败切换下一个
                last_error = e
                logger.warning('下载源失败（%s）: %s', url, e)
                continue

        raise DownloadError(f'所有下载源均失败: {last_error}')
    finally:
        if own_client:
            await client.aclose()
