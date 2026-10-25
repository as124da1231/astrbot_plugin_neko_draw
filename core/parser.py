# -*- coding: utf-8 -*-
"""提示词与参数解析。

格式: <触发词> <提示词内容> --参数1 值1 --参数2 值2
- 参数值缺省视为 true
- 值含空格时用引号包裹
- 参数可以出现在任意位置（触发词之后）
- 参数键值完全透传给模板（由模型模板决定哪些参数有效）
"""
import re
import shlex
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedPrompt:
    """解析结果"""
    text: str = ""                    # 去除参数后的提示词文本（触发词已去除）
    params: dict = field(default_factory=dict)   # 解析出的参数
    trigger: str = ""                 # 命中的触发词
    user_body: str = ""               # 触发词与 --参数 之后、用于替换 {{user_text}} 的原始用户文本
    prompt_params: list = field(default_factory=list)  # 依次出现的 --参数 键（用于判定“哪个同名参数最终生效”）
    user_params: dict = field(default_factory=dict)    # 仅来自用户消息的参数


def _split_tokens(text: str) -> list[str]:
    """按 shell 规则切分；引号不闭合时退化为普通分割，保证绝不抛错。"""
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def parse_params(text: str) -> tuple[str, dict]:
    """从文本中提取 --key value 参数。

    返回 (剩余文本, 参数字典)。参数值缺省时为字符串 "true"。
    """
    return _parse_params_with_order(text)[0]


def _parse_params_with_order(text: str) -> tuple[tuple[str, dict], list[str]]:
    """解析参数，并返回参数键的出现顺序（用于同名参数“后者覆盖前者”的判定）。"""
    tokens = _split_tokens(text)
    parts: list[str] = []
    params: dict[str, str] = {}
    order: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("--") and len(token) > 2:
            key = token[2:].strip()
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                params[key] = tokens[i + 1]
                i += 2
            else:
                params[key] = "true"
                i += 1
            order.append(key)
        else:
            parts.append(token)
            i += 1
    return (" ".join(parts).strip(), params), order


_AT_MENTION_RE = re.compile(r"@\S+")


def _normalize_trigger_join(text: str) -> str:
    """把「触发词@某人」补成「触发词 @某人」，让后续按空格切分的匹配逻辑生效。

    AstrBot 渲染 @ 时可能不留空格，若不补，触发词会被当成整段文本的一部分。
    """
    return re.sub(r"(?<=[^\s@])@", " @", str(text or ""))


def strip_at_mentions(text: str, mentions=None) -> str:
    """去掉文本里的 @昵称/@QQ 片段，只留下用户真正写的补充说明。

    封面场景：``提取@群友`` 里的 ``@群友`` 是给对方看的，不该当成提示词内容。

    参数透传给 :func:`core.text_utils.strip_at_mentions`，全插件只保留一份实现。
    """
    from .text_utils import strip_at_mentions as _strip

    return _strip(text, mentions or ())


def _visible_text(template: str, substitute: str) -> str:
    """把模板渲染成最终提示词。

    优先替换所有 {{user_text}}；模板不含占位符时把用户文本追加到末尾。
    用户正文为空时不会把占位符本身（或残留的空格）交给模型——否则模型会
    按字面量画 “{{user_text}}”。
    """
    substitute = (substitute or "").strip()
    if "{{user_text}}" not in template:
        if not substitute:
            return " ".join(template.split())
        return " ".join(f"{template} {substitute}".split())
    if substitute:
        return " ".join(template.replace("{{user_text}}", substitute).split())
    # 用户正文为空：去掉占位符以及由此产生的多余空白
    return " ".join(template.replace("{{user_text}}", " ").split())


def parse_prompt_message(
    message_text: str,
    prompt_configs: list[str],
    user_text_override: Optional[str] = None,
) -> Optional[ParsedPrompt]:
    """按预设提示词匹配消息。

    prompt_configs 元素格式: "<触发词> <提示词模板> --参数1 值1"
    模板中的 {{user_text}} 会被消息中触发词之后的用户文本替换。
    消息中携带的 --参数 会覆盖预设中的同名参数。

    user_text_override 不为 None 时，用它替换 {{user_text}}（用于生图前的文字预处理），
    但触发词与 --参数 仍从原消息解析。

    返回 None 表示没有匹配的触发词。
    """
    if not message_text or not prompt_configs:
        return None
    from .text_utils import strip_leading_at

    # 先剥掉开头的 @ 提及（@铃 提取 / @某人 nd 一只猫），再补触发词后的空格
    message_text = _normalize_trigger_join(strip_leading_at(message_text))
    # 匹配触发词：先按最长触发词匹配，避免较短触发词抢先命中
    candidates = []
    for cfg in prompt_configs:
        if not cfg or not cfg.strip():
            continue
        cfg = cfg.strip()
        tokens = cfg.split(None, 1)
        if not tokens:
            continue
        trigger = tokens[0]
        rest_cfg = tokens[1] if len(tokens) > 1 else ""
        if message_text == trigger or message_text.startswith(trigger + " "):
            candidates.append((len(trigger), trigger, rest_cfg))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, trigger, rest_cfg = candidates[0]

    user_text = message_text[len(trigger):].strip()
    # 用户文本中的参数
    (user_body, user_params), user_order = _parse_params_with_order(user_text)
    # 预设提示词模板中的参数
    (cfg_body, cfg_params), cfg_order = _parse_params_with_order(rest_cfg)
    # 用于替换 {{user_text}} 的内容：默认取用户正文，LLM 拆解时改由调用方提供
    substitute = user_body if user_text_override is None else user_text_override

    final_text = _visible_text(cfg_body, substitute)

    # 参数合并：用户参数覆盖预设参数
    merged = dict(cfg_params)
    merged.update(user_params)
    return ParsedPrompt(
        text=final_text,
        params=merged,
        trigger=trigger,
        user_body=user_body,
        # 预设模板里的参数在前、用户消息里的在后，同名参数后者生效
        prompt_params=list(cfg_order) + list(user_order),
        user_params=dict(user_params),
    )
