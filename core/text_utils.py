# -*- coding: utf-8 -*-
"""纯文本小工具：@ 提及剥离、场景文案、模型输出清洗。

这些东西以前散落在 ``main.py`` 的类方法里，既难复用也无法单独测试。
全部改成无状态函数，消息处理模块只负责“组装”。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# 匹配 `@昵称` / `@QQ号`。昵称里常有英文数字下划线，因此用“非空白”而不是
# “非 ASCII”。后面紧跟触发词且没有空格的情况（``@铃提取``）见 strip_at_mentions。
_AT_MENTION_RE = re.compile(r"@\S+")

# 「LLM 拆解 tag」默认提示词模板；必须包含 {{user_text}} 占位符。
DEFAULT_LLM_TAG_PROMPT = (
    "你是 AI 绘图提示词工程师。请把用户输入的中文或口语描述拆解为英文 Danbooru 风格绘图 tag，"
    "用英文逗号分隔；只输出 tag 本身，不要解释、不要编号、不要引号、不要换行。\n"
    "用户输入：{{user_text}}"
)

# 拆解结果里的拒答特征。对话模型遇到敏感内容时，可能不回 tag 而回一段
# “抱歉，我不能……”的说明；这种文本一旦被当成绘图提示词，出图必然失败，
# 所以要在进入绘图前识别出来并回退为原文。
REFUSAL_MARKERS = (
    "抱歉", "对不起", "很遗憾", "不能生成", "无法生成", "不能提供", "无法提供",
    "不能协助", "无法协助", "不能帮助", "无法帮助", "无法完成", "不能完成",
    "无法创建", "不能创建", "无法满足", "不能满足", "我不会", "我无法", "我不能",
    "违反", "违规", "政策", "准则", "不当内容", "敏感内容", "拒绝",
    "i'm sorry", "i am sorry", "i apologize", "i can't", "i cannot", "i can not",
    "i'm unable", "i am unable", "i won't", "i will not", "can't assist",
    "cannot assist", "can't help", "cannot help", "can't create", "cannot create",
    "can't generate", "cannot generate", "unable to comply", "against my",
    "violates", "not appropriate", "as an ai",
)
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def strip_at_mentions(text: str, mentions: Iterable[str] = ()) -> str:
    """去掉文本里的 @ 提及，只留下用户真正写的补充说明。

    ``mentions`` 传入消息链里真实 @ 的显示文本（如 ``@铃``）时优先精确替换：
    这样 ``@铃提取`` 剥完是 ``提取``，而不是被正则整段吃掉变成空串。

    正则兜底会吃掉 ``@`` 后面整段非空白文本：拿不到真实昵称时
    ``@铃提取`` 会连触发词一起剥掉，这是纯文本输入的上限；
    如需精确剥离，请通过 ``mentions`` 传入消息链里的真实昵称。
    """
    raw = str(text or "")
    # 1) 精确移除消息链里的真实提及（长的先替换，避免前缀互相吃掉）
    for token in sorted({str(m) for m in mentions if str(m or "").strip()}, key=len, reverse=True):
        raw = raw.replace(token, " ")
    # 2) 正则兜底：剥掉 @ 与昵称（@\S+），替换成单个空格。
    #    非空白昵称没有边界，正则无法判断昵称在哪结束，因此：
    #    - 有真实提及可用时，这一步只兜底“没进消息链”的残留；
    #    - 纯文本输入（@铃提取）会连触发词一起剥掉，属预期上限：拿不到昵称就没法区分。
    raw = _AT_MENTION_RE.sub(" ", raw)
    # 注意：这里只 strip，不折叠内部空白 —— 调用方后面会用 ``[len(trigger):]``
    # 按位置截取正文，折叠空白会让下标错位。
    return raw.strip()


def strip_leading_at(text: str) -> str:
    """剥离开头的 @ 提及，保留正文。

    ``@铃 提取`` / ``@某人 扩写 一只猫`` 这类写法在 QQ 里很常见：AstrBot 把
    @ 一起拼进了 message_str，于是所有“以触发词开头”的判断都会失败。
    这里只在匹配前剥掉开头的提及，不影响 ``提取@某人`` 这种正文里的 @。

    注意：没有空格时（``@铃提取``）无法判断昵称边界，会连同触发词一起剥掉；
    这种情况依赖消息链里的 At 组件提供真实昵称（见 ImageCollector.mention_texts）。
    另外 ``@`` 后面跟的是 QQ 号一样能剥干净；分隔符用空格或全角空格都行。
    """
    value = str(text or "").lstrip()
    while value.startswith("@"):
        # @ 后面到第一个空白字符为止都算昵称/QQ号（含全角空格、制表符）
        index = 1
        while index < len(value) and not value[index].isspace():
            index += 1
        value = value[index:].lstrip()
    return value


# 兼容旧名
strip_leading_mentions = strip_leading_at


def scene_message(conf: Any, key: str, scene: str, trigger: str = "", model: str = "") -> str:
    """读取「开始提示 / 结束提示」等场景文案。

    ``scene`` 为 ``expand`` / ``extract`` 时优先使用该分区专属键
    （``expand_start_message``），没有再回落到通用键。
    """
    candidates = []
    if scene:
        candidates.append(f"{scene}_{key}")
    candidates.append(key)
    template = ""
    for name in candidates:
        value = conf.get(name) if hasattr(conf, "get") else None
        if value:
            template = str(value)
            break
    if not template:
        return ""
    try:
        return template.format(trigger=trigger or "", model=model or "")
    except (KeyError, IndexError, ValueError):
        return template


def detect_tag_refusal(text: str) -> str:
    """检查 LLM 拆解结果是否像一段拒答说明；是则返回原因，否则返回空字符串。

    只看开头一小段：拒答几乎总是“抱歉 / I'm sorry ……”开头，而正常 tag 里
    即使偶有短语也更靠后，这样既能抓到拒答，又不会误伤长 tag 文本。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    head = raw[:200]
    lowered = head.lower()
    for marker in REFUSAL_MARKERS:
        if marker in lowered:
            return f"疑似拒答（{marker}）"
    # 正常输出应是英文 tag；出现中文说明模型没按格式输出，多半在解释或道歉。
    # 这里宁可误判也不要放过：被误判时上层会回退为原文提示词继续出图，
    # 而放过一段“抱歉……”会被直接当成提示词提交，结果必然是废图。
    cjk = len(_CJK_RE.findall(head))
    if cjk:
        return f"输出含中文（{cjk} 个字符）"
    return ""


def strip_tag_noise(text: str) -> str:
    """清理拆解结果里常见的包装：代码块围栏、项目符号、结尾句号。"""
    raw = str(text or "").strip()
    # ```text ... ``` 代码块
    if raw.startswith("```"):
        raw = raw.strip("`")
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    # 去掉行首的 “- ”、“1. ”、“* ” 等序号/项目符号
    cleaned = []
    for line in lines:
        line = re.sub(r"^(?:[-*•]|\d+[.)、])\s*", "", line)
        cleaned.append(line)
    joined = ", ".join(cleaned).strip()
    # 整段被引号包住时去掉引号，再收拾结尾的句号/逗号
    if len(joined) >= 2 and joined[0] == joined[-1] and joined[0] in "\"'“”":
        joined = joined[1:-1].strip()
    return joined.rstrip(".。,，").strip()


def clean_model_text(text: str) -> str:
    """去掉模型返回文本外层包裹的引号（扩写 / 提取结果）。"""
    value = str(text or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'“”":
        return value[1:-1].strip()
    return value


def split_command(text: str, commands: Iterable[str]) -> Optional[str]:
    """去掉命令前缀（兼容带/与不带/），返回剩余部分；不是该命令返回 None。"""
    value = str(text or "").strip()
    for cmd in commands:
        for prefix in (f"/{cmd}", cmd):
            if value == prefix:
                return ""
            if value.startswith(prefix) and value[len(prefix)].isspace():
                return value[len(prefix):].strip()
    return None
