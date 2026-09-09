# -*- coding: utf-8 -*-
"""提示词与参数解析。

格式: <触发词> <提示词内容> --参数1 值1 --参数2 值2
- 参数值缺省视为 true
- 值含空格时用引号包裹
- 参数可以出现在任意位置（触发词之后）
- 参数键值完全透传给模板（由模型模板决定哪些参数有效）
"""
import shlex
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ParsedPrompt:
    """解析结果"""
    text: str = ""                    # 去除参数后的提示词文本（触发词已去除）
    params: dict = field(default_factory=dict)   # 解析出的参数
    trigger: str = ""                 # 命中的触发词


def parse_params(text: str) -> tuple[str, dict]:
    """从文本中提取 --key value 参数。

    返回 (剩余文本, 参数字典)。参数值缺省时为字符串 "true"。
    """
    try:
        tokens = shlex.split(text, posix=True)
    except ValueError:
        # 引号不闭合时退化为普通分割
        tokens = text.split()

    parts: list[str] = []
    params: dict[str, str] = {}
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
        else:
            parts.append(token)
            i += 1
    return " ".join(parts).strip(), params


def parse_prompt_message(message_text: str, prompt_configs: list[str]) -> Optional[ParsedPrompt]:
    """按预设提示词匹配消息。

    prompt_configs 元素格式: "<触发词> <提示词模板> --参数1 值1"
    模板中的 {{user_text}} 会被消息中触发词之后的用户文本替换。
    消息中携带的 --参数 会覆盖预设中的同名参数。

    返回 None 表示没有匹配的触发词。
    """
    if not message_text or not prompt_configs:
        return None
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
    user_body, user_params = parse_params(user_text)
    # 预设提示词模板中的参数
    cfg_body, cfg_params = parse_params(rest_cfg)

    # 用配置的提示词模板，没有模板时直接用用户文本
    final_text = cfg_body
    if "{{user_text}}" in final_text:
        final_text = final_text.replace("{{user_text}}", user_body)
    else:
        # 模板不含占位符时，把用户文本拼在末尾（保持可预测）
        if user_body:
            final_text = f"{final_text} {user_body}".strip()

    # 参数合并：用户参数覆盖预设参数
    merged = dict(cfg_params)
    merged.update(user_params)
    return ParsedPrompt(text=final_text, params=merged, trigger=trigger)
