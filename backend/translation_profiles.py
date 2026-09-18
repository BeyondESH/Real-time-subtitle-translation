"""
翻译 prompt 与采样参数 profile（按模型族内置，MUST NOT 开放用户改写）

依据（design.md D3/D7）：
- 专用模型的官方 prompt 格式对效果至关重要（LunaTranslator 为 Sakura 系
  内置不可自定义的专用接口即此原因）；
- 每族采样参数取官方推荐值；
- 具思考能力的模型族默认关闭思考（服务端 `--jinja` + 请求级
  `chat_template_kwargs`），避免思考 token 拖垮实时延迟。
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class TranslationProfile:
    key: str
    # 指令模板：{target_name} 为目标语言显示名（language_codes 显式映射），{text} 为源文本
    instruction_template: str
    system_prompt: Optional[str]
    # 采样参数（llama-server /v1/chat/completions 请求体字段）
    sampling: Dict[str, float] = field(default_factory=dict)
    max_tokens_cap: int = 4096  # 输出硬上限（另按输入长度动态收紧）
    # llama-server 启动附加参数（如 --jinja）
    server_extra_args: Tuple[str, ...] = ()
    # 请求级 chat_template_kwargs（如 {"enable_thinking": False}）
    chat_template_kwargs: Optional[Dict[str, object]] = None


PROFILES: Dict[str, TranslationProfile] = {
    # Hy-MT2 官方推荐：极简指令，无 system prompt；参数取官方推荐值
    'hy-mt2': TranslationProfile(
        key='hy-mt2',
        instruction_template=(
            '将以下文本翻译为{target_name}，注意只需要输出翻译后的结果，不要额外解释：\n{text}'
        ),
        system_prompt=None,
        sampling={
            'temperature': 0.7,
            'top_p': 0.6,
            'top_k': 20,
            'repetition_penalty': 1.05,
        },
    ),
    # Qwen3 通用：关思考 + presence_penalty 1.5（量化模型防重复的官方建议）
    'qwen3': TranslationProfile(
        key='qwen3',
        instruction_template=(
            '将以下文本翻译为{target_name}。只输出翻译结果，不要任何解释、标注或引号。\n{text}'
        ),
        system_prompt=None,
        sampling={
            'temperature': 0.7,
            'top_p': 0.8,
            'top_k': 20,
            'presence_penalty': 1.5,
        },
        server_extra_args=('--jinja',),
        chat_template_kwargs={'enable_thinking': False},
    ),
}


def get_profile(key: str) -> TranslationProfile:
    profile = PROFILES.get(key)
    if profile is None:  # pragma: no cover - 注册表引用完整性由测试守护
        raise KeyError(f'未知翻译 profile: {key}')
    return profile


def build_messages(profile: TranslationProfile, text: str, target_name: str) -> list:
    """按 profile 构造 chat messages（目标语言名经显式映射注入）"""
    messages = []
    if profile.system_prompt:
        messages.append({'role': 'system', 'content': profile.system_prompt})
    messages.append({
        'role': 'user',
        'content': profile.instruction_template.format(
            target_name=target_name, text=text
        ),
    })
    return messages
