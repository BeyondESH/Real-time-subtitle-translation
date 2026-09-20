"""
ASR 模型注册表与下载编排（Fun-ASR-Nano INT8，sherpa-onnx 运行时）

- 模型标识：`sherpa-onnx-funasr-nano-int8-2025-12-30`（单引擎模型，无档位）
- 资产形态：多文件组（encoder_adaptor / llm / embedding 三个 INT8 ONNX +
  Qwen3-0.6B tokenizer 三件套），落地 `~/.cache/subtitle-translator/funasr-nano/<model-id>/`
- 下载源：HuggingFace 官方 → hf-mirror 镜像（逐文件尝试，沿用 model_downloader 的
  流式 + Range 断点续传 + 校验机制）
- 校验：字节数强校验；LFS 文件额外 sha256 强校验（来源：HF 仓库 LFS 元数据，
  commit pin 见 ASR_MODEL_REVISION）；merges.txt 为非 LFS 文件，仅按大小校验
- 进度：跨文件按字节数加权聚合，复用 model_progress 形状（name/percent/message）

许可说明：sherpa-onnx 运行时代码为 Apache-2.0；模型权重许可在 HF 仓库未标注，
待 Phase 0 / 发布前核实归档（见本变更 tasks 0.4）。
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

from model_downloader import (
    DownloadError,
    ProgressCallback,
    _report,
    _sha256_file,
)

logger = logging.getLogger(__name__)

# 引擎模型标识（协议 change_model 的唯一合法值；config asr.model 的默认值）
ENGINE_MODEL_ID = 'funasr-nano'
ENGINE_DISPLAY_NAME = 'Fun-ASR-Nano'

# sherpa-onnx 模型资产组标识（k2-fsa 发布命名）
ASR_MODEL_ID = 'sherpa-onnx-funasr-nano-int8-2025-12-30'
# HuggingFace 仓库与 pin 的 commit（保证下载可复现；sha256 与该 commit 的 LFS 元数据一致）
ASR_MODEL_REPO = 'csukuangfj/sherpa-onnx-funasr-nano-int8-2025-12-30'
ASR_MODEL_REVISION = '6f16bd378457e13f36ccf3910df9017f96c346fb'

ASR_MODEL_LICENSE = '待核实（发布前归档，见 tasks 0.4）'
ASR_MODEL_LICENSE_NOTE = (
    'sherpa-onnx 运行时代码 Apache-2.0；模型权重许可 HF 仓库未标注，'
    '发布前须核实并归档（design D2 / tasks 0.4）。'
)


@dataclass(frozen=True)
class AsrModelFile:
    """一个模型资产文件条目（rel_path 同时是仓库内路径与落地相对路径）"""

    rel_path: str
    size_bytes: int
    sha256: Optional[str] = None  # None → 非 LFS 文件，仅按大小校验

    @property
    def display_name(self) -> str:
        """下载器进度回调所需的显示名（model_downloader._download_from 契约）"""
        return f'{ENGINE_DISPLAY_NAME} {self.rel_path}'


# 资产清单（大小与 sha256 来源：HF 仓库 LFS 元数据 @ ASR_MODEL_REVISION）
ASR_MODEL_FILES: Tuple[AsrModelFile, ...] = (
    AsrModelFile(
        rel_path='encoder_adaptor.int8.onnx',
        size_bytes=237_792_748,
        sha256='f36dea2e30fbc33b5db1d7a7265cc976c5e5586c77b042d5adb1ad27c72db422',
    ),
    AsrModelFile(
        rel_path='llm.int8.onnx',
        size_bytes=600_356_593,
        sha256='dfbf9aa3be41bccc257587f151e15c63fbe1b549f2b517f5ccd5bdce3bf4322a',
    ),
    AsrModelFile(
        rel_path='embedding.int8.onnx',
        size_bytes=155_584_380,
        sha256='95e61cd0c9c3b9543339a4cf973c95c116815e745ccc1e0285cbd81f76d18644',
    ),
    AsrModelFile(
        rel_path='Qwen3-0.6B/tokenizer.json',
        size_bytes=11_422_654,
        sha256='aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4',
    ),
    AsrModelFile(
        rel_path='Qwen3-0.6B/vocab.json',
        size_bytes=2_776_833,
        sha256='ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910',
    ),
    AsrModelFile(
        rel_path='Qwen3-0.6B/merges.txt',
        size_bytes=1_671_853,
        sha256=None,  # 非 LFS 文件无内容寻址哈希，仅按大小校验
    ),
)

_ASR_TOTAL_BYTES = sum(f.size_bytes for f in ASR_MODEL_FILES)


def asr_cache_root(cache_root: Optional[Path] = None) -> Path:
    """ASR 模型缓存根目录（与翻译模型缓存同源：~/.cache/subtitle-translator）"""
    base = cache_root or (Path.home() / '.cache' / 'subtitle-translator' / 'funasr-nano')
    return base / ASR_MODEL_ID


def asr_file_path(rel_path: str, cache_root: Optional[Path] = None) -> Path:
    return asr_cache_root(cache_root) / rel_path


def _file_ready(path: Path, f: AsrModelFile) -> bool:
    try:
        return path.exists() and path.stat().st_size == f.size_bytes
    except OSError:
        return False


def is_asr_model_downloaded(cache_root: Optional[Path] = None) -> bool:
    """全部资产就绪（文件存在且字节数吻合；sha256 校验在下载路径执行）"""
    root = asr_cache_root(cache_root)
    return all(_file_ready(root / f.rel_path, f) for f in ASR_MODEL_FILES)


def asr_model_paths(cache_root: Optional[Path] = None) -> dict:
    """from_funasr_nano 所需路径（tokenizer 为 Qwen3-0.6B 目录）"""
    root = asr_cache_root(cache_root)
    return {
        'encoder_adaptor': root / 'encoder_adaptor.int8.onnx',
        'llm': root / 'llm.int8.onnx',
        'embedding': root / 'embedding.int8.onnx',
        'tokenizer': root / 'Qwen3-0.6B',
    }


def _resolve_file_sources(rel_path: str, source: str = 'auto') -> list:
    """单文件下载 URL 列表（官方 → 镜像；与翻译下载器同一来源词汇）"""
    official = (
        f'https://huggingface.co/{ASR_MODEL_REPO}/resolve/{ASR_MODEL_REVISION}/{rel_path}'
    )
    mirror = (
        f'https://hf-mirror.com/{ASR_MODEL_REPO}/resolve/{ASR_MODEL_REVISION}/{rel_path}'
    )
    if source == 'huggingface':
        return [official]
    if source == 'hf-mirror':
        return [mirror]
    return [official, mirror]


async def ensure_asr_model_downloaded(
    progress: Optional[ProgressCallback] = None,
    source: str = 'auto',
    cache_root: Optional[Path] = None,
    client=None,
) -> Path:
    """
    确保全部模型资产就绪（缺失文件逐个下载；跨文件进度按字节加权聚合）。

    Raises:
        DownloadError: 任一文件所有源均失败 / 校验失败
    """
    import httpx

    root = asr_cache_root(cache_root)
    if is_asr_model_downloaded(cache_root):
        _report(progress, ENGINE_DISPLAY_NAME, 100.0, '识别模型已就绪')
        return root

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(60.0, connect=15.0),
        )
    try:
        done_bytes = sum(
            f.size_bytes for f in ASR_MODEL_FILES
            if _file_ready(root / f.rel_path, f)
        )
        for f in ASR_MODEL_FILES:
            final = root / f.rel_path
            if _file_ready(final, f):
                continue
            final.parent.mkdir(parents=True, exist_ok=True)
            part = final.with_name(final.name + '.part')
            base = done_bytes

            def make_adapter(file: AsrModelFile, done: int) -> ProgressCallback:
                """单文件进度 → 全局加权进度适配器"""
                def adapter(_name: str, pct: float, _msg: str) -> None:
                    overall = min(
                        99.0, (done + file.size_bytes * pct / 100.0) * 100.0 / _ASR_TOTAL_BYTES
                    )
                    mb = int((done + file.size_bytes * pct / 100.0) / (1024 * 1024))
                    _report(progress, ENGINE_DISPLAY_NAME, overall,
                            f'下载中 {overall:.0f}%（{mb}MB / {_ASR_TOTAL_BYTES // (1024 * 1024)}MB）')
                return adapter

            urls = _resolve_file_sources(f.rel_path, source)
            if not urls:
                raise DownloadError(f'没有可用的下载源（source={source}）')
            last_error: Optional[Exception] = None
            for url in urls:
                _report(progress, ENGINE_DISPLAY_NAME,
                        done_bytes * 100.0 / _ASR_TOTAL_BYTES, '开始下载识别模型…')
                try:
                    from model_downloader import _download_from

                    await _download_from(client, url, part, f, make_adapter(f, base))
                    if part.stat().st_size != f.size_bytes:
                        raise DownloadError(
                            f'文件大小不符（期望 {f.size_bytes}，实际 {part.stat().st_size}）'
                        )
                    if f.sha256:
                        digest = await asyncio.to_thread(_sha256_file, part)
                        if digest != f.sha256:
                            logger.warning(
                                'sha256 校验失败（%s，期望 %s… 实际 %s…），丢弃并尝试其它源',
                                f.rel_path, f.sha256[:12], digest[:12],
                            )
                            part.unlink(missing_ok=True)
                            last_error = DownloadError(
                                f'sha256 校验失败（{f.rel_path}，实际 {digest[:12]}…）'
                            )
                            continue
                    os.replace(part, final)
                    done_bytes += f.size_bytes
                    _report(progress, ENGINE_DISPLAY_NAME,
                            done_bytes * 100.0 / _ASR_TOTAL_BYTES,
                            f'已完成 {f.rel_path}')
                    last_error = None
                    break
                except Exception as e:  # noqa: BLE001 - 单源失败切换下一个
                    last_error = e
                    logger.warning('下载源失败（%s）: %s', url, e)
                    continue
            if last_error is not None:
                raise DownloadError(f'识别模型文件下载失败（{f.rel_path}）: {last_error}')

        _report(progress, ENGINE_DISPLAY_NAME, 100.0, '识别模型下载完成')
        return root
    finally:
        if own_client:
            await client.aclose()
