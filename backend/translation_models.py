"""
翻译模型注册表（随包静态清单）

- 条目字段：id / 显示名 / 下载来源（仓库 + pin 的 revision）/ 文件名 /
  字节数 / sha256 / 许可证 / prompt-profile 引用
- 下载 URL 由 `resolve_download_sources` 按来源策略生成
  （HuggingFace 官方 / HF 镜像 / ModelScope 模板）
- 模型文件路径：`~/.cache/subtitle-translator/translation/<model-id>/<filename>`

默认模型为 Hy-MT2-1.8B（Apache-2.0，pin commit a0c709d…，见
`third_party_licenses/NOTES.md` 的许可证历史说明）。
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 默认模型（design.md D3）
DEFAULT_MODEL_ID = 'hy-mt2-1.8b-q4km'


@dataclass(frozen=True)
class TranslationModel:
    """一个可下载/可切换的翻译模型条目"""

    id: str
    display_name: str
    repo: str
    revision: str  # pin 的 commit（保证下载可复现）
    filename: str
    size_bytes: int
    sha256: str
    license: str
    license_note: str
    profile: str  # translation_profiles.PROFILES 的键
    # ModelScope 镜像的仓库路径（未验证/未提供时为 None，源解析时跳过）
    modelscope_repo: Optional[str] = None


REGISTRY: Tuple[TranslationModel, ...] = (
    TranslationModel(
        id='hy-mt2-1.8b-q4km',
        display_name='Hy-MT2 1.8B（推荐）',
        repo='tencent/Hy-MT2-1.8B-GGUF',
        revision='a0c709d9fac510f2c807aa3af52872340dc37a4a',
        filename='Hy-MT2-1.8B-Q4_K_M.gguf',
        size_bytes=1_133_080_448,
        sha256='dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699',
        license='Apache-2.0',
        license_note=(
            '当前 main 为 Apache-2.0；原始发布版为腾讯 HY 社区许可（含地域限制），'
            '后经 license metadata 重新授权。归档文本见 third_party_licenses/。'
        ),
        profile='hy-mt2',
    ),
    TranslationModel(
        id='hy-mt2-7b-q4km',
        display_name='Hy-MT2 7B（高质量）',
        repo='tencent/Hy-MT2-7B-GGUF',
        revision='ab8472660ac61fac25f1af43fac2599d52a8a775',
        filename='Hy-MT2-7B-Q4_K_M.gguf',
        size_bytes=4_624_648_896,
        sha256='9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b',
        license='Apache-2.0',
        license_note='同族模型，许可证历史与 1.8B 相同（原始版社区许可 → 现 Apache-2.0）。',
        profile='hy-mt2',
    ),
    TranslationModel(
        id='qwen3-1.7b-q4km',
        display_name='Qwen3 1.7B（通用/多语兜底）',
        repo='bartowski/Qwen_Qwen3-1.7B-GGUF',
        revision='dcb19155b962dbb6389f4691a982043a8e651022',
        filename='Qwen_Qwen3-1.7B-Q4_K_M.gguf',
        size_bytes=1_282_439_584,
        sha256='72c5c3cb38fa32d5256e2fe30d03e7a64c6c79e668ad84057e3bd66e250b24fb',
        license='Apache-2.0',
        license_note='Qwen3 系 Apache-2.0；bartowski 为社区 GGUF 转换。',
        profile='qwen3',
    ),
)

_BY_ID: Dict[str, TranslationModel] = {m.id: m for m in REGISTRY}


def get_model(model_id: str) -> Optional[TranslationModel]:
    """按 id 查模型；未知 id 返回 None（调用方负责 invalid_llm 回执）"""
    return _BY_ID.get(model_id)


def default_model() -> TranslationModel:
    model = get_model(DEFAULT_MODEL_ID)
    if model is None:  # pragma: no cover - 注册表完整性由测试守护
        raise RuntimeError(f'注册表缺少默认模型: {DEFAULT_MODEL_ID}')
    return model


def translation_cache_root() -> Path:
    """翻译模型缓存根目录（与其它缓存同源：~/.cache/subtitle-translator）"""
    return Path.home() / '.cache' / 'subtitle-translator' / 'translation'


def model_dir(model: TranslationModel, cache_root: Optional[Path] = None) -> Path:
    return (cache_root or translation_cache_root()) / model.id


def model_path(model: TranslationModel, cache_root: Optional[Path] = None) -> Path:
    return model_dir(model, cache_root) / model.filename


def is_downloaded(model: TranslationModel, cache_root: Optional[Path] = None) -> bool:
    """文件存在且字节数吻合（sha256 校验在下载/加载路径中执行）"""
    p = model_path(model, cache_root)
    try:
        return p.exists() and p.stat().st_size == model.size_bytes
    except OSError:
        return False


def resolve_download_sources(
    model: TranslationModel, source: str = 'auto'
) -> List[str]:
    """
    按来源策略生成下载 URL 列表（按优先级排列）。

    - 'huggingface'：仅官方
    - 'hf-mirror'：仅镜像
    - 'modelscope'：仅 ModelScope（模型未提供 ms 仓库时返回空列表）
    - 'auto'：官方 → 镜像 → ModelScope（逐个尝试，前者失败即切换）
    """
    official = f'https://huggingface.co/{model.repo}/resolve/{model.revision}/{model.filename}'
    mirror = f'https://hf-mirror.com/{model.repo}/resolve/{model.revision}/{model.filename}'
    ms: Optional[str] = None
    if model.modelscope_repo:
        ms = (
            f'https://modelscope.cn/api/v1/models/{model.modelscope_repo}/repo'
            f'?Revision=master&FilePath={model.filename}'
        )

    if source == 'huggingface':
        return [official]
    if source == 'hf-mirror':
        return [mirror]
    if source == 'modelscope':
        return [ms] if ms else []
    # auto
    return [u for u in (official, mirror, ms) if u]
