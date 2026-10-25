# -*- coding: utf-8 -*-
"""RunningHub ComfyUI 工作流的解析与 nodeInfoList 构造。

RunningHub 云端不提供标准 ComfyUI 的 /prompt、/history 接口，也不提供“读取某个
工作流节点列表”的接口；官方建议直接导出“API 格式”的工作流 JSON 来查看可用参数。
因此本模块做两件事：

1. ``analyze_workflow``：解析用户上传的 API 格式工作流 JSON，列出所有“可以在 API
   里设置的字段”（即节点 inputs 中的标量值；被连线引用的字段值是 list，不能在 API
   里直接改），并给出一个用途猜测，供 WebUI 让用户下拉确认。
2. ``build_node_info_list``：按用户确认后的映射，把提示词/尺寸/种子等拼装成
   RunningHub /run/workflow 需要的 nodeInfoList。

工作流 JSON 的顶层键就是 nodeId；每个节点形如::

    "54": {"inputs": {"text": "..."}, "class_type": "CLIPTextEncode", "_meta": {"title": "..."}}
"""
from __future__ import annotations

import random
from typing import Any

# 用途取值（WebUI 下拉与此保持一致）
ROLE_PROMPT = "prompt"
ROLE_NEGATIVE = "negative"
ROLE_WIDTH = "width"
ROLE_HEIGHT = "height"
ROLE_SEED = "seed"
ROLE_STEPS = "steps"
ROLE_CFG = "cfg"
ROLE_SAMPLER = "sampler"
ROLE_SCHEDULER = "scheduler"
ROLE_COUNT = "count"
ROLE_IMAGE = "image"
ROLE_FIXED = "fixed"
ROLE_IGNORE = "ignore"

# —— 画风 / 风格 / 提示词结构 ——
ROLE_ARTIST_STYLE = "artist_style"
ROLE_ARTIST_STYLE_WEIGHT = "artist_style_weight"
ROLE_ARTIST_PREFIX = "artist_prefix"
ROLE_GELBOORU_STYLE = "gelbooru_style"
ROLE_GELBOORU_STYLE_WEIGHT = "gelbooru_style_weight"
ROLE_POSITIVE_PREFIX = "positive_prefix"
ROLE_POSITIVE_SUFFIX = "positive_suffix"
ROLE_PROMPT_PART_3 = "prompt_part_3"
ROLE_PROMPT_PART_4 = "prompt_part_4"
ROLE_PROMPT_PART_5 = "prompt_part_5"
ROLE_PROMPT_PART_6 = "prompt_part_6"
ROLE_DELIMITER = "delimiter"
ROLE_MERGE_SEPARATOR = "merge_separator"
ROLE_CLEAN_WHITESPACE = "clean_whitespace"

# —— 模型 / LoRA / 采样器高级参数 ——
ROLE_MODEL_NAME = "model_name"
ROLE_LORA_NAME = "lora_name"
ROLE_LORA_STRENGTH = "lora_strength"
ROLE_LORA_STRENGTH_CLIP = "lora_strength_clip"
ROLE_CLIP_NAME = "clip_name"
ROLE_VAE_NAME = "vae_name"
ROLE_DENOISE = "denoise"
ROLE_SHIFT = "shift"
ROLE_TEACACHE_THRESHOLD = "teacache_threshold"
ROLE_TEACACHE_ADAPTIVE = "teacache_adaptive"
ROLE_SAGE_ATTENTION = "sage_attention"
ROLE_FILENAME_PREFIX = "filename_prefix"

# —— 条件分支 / 常量 / 表达式类节点（CR Text、StringContains、MathExpression 等）——
# 这类节点本身不生成图片，值往往被下游节点连线引用（如「尺寸」文本 → 是否竖版 →
# 宽/高表达式）。把它们的固定值暴露出来，用户就能在 WebUI 里直接改“横版 / 竖版”。
ROLE_TEXT_CONST = "text_const"

ROLE_LABELS = {
    ROLE_PROMPT: "正向提示词",
    ROLE_NEGATIVE: "负面提示词",
    ROLE_WIDTH: "宽度",
    ROLE_HEIGHT: "高度",
    ROLE_SEED: "随机种子",
    ROLE_STEPS: "采样步数",
    ROLE_CFG: "CFG 强度",
    ROLE_SAMPLER: "采样器",
    ROLE_SCHEDULER: "调度器",
    ROLE_COUNT: "生成数量",
    ROLE_IMAGE: "参考图",
    ROLE_ARTIST_STYLE: "画师风格",
    ROLE_ARTIST_STYLE_WEIGHT: "画师风格权重",
    ROLE_ARTIST_PREFIX: "画师前缀开关",
    ROLE_GELBOORU_STYLE: "Gelbooru 风格",
    ROLE_GELBOORU_STYLE_WEIGHT: "Gelbooru 风格权重",
    ROLE_POSITIVE_PREFIX: "正向前缀",
    ROLE_POSITIVE_SUFFIX: "正向后缀",
    ROLE_PROMPT_PART_3: "提示词分区 3",
    ROLE_PROMPT_PART_4: "提示词分区 4",
    ROLE_PROMPT_PART_5: "提示词分区 5",
    ROLE_PROMPT_PART_6: "提示词分区 6",
    ROLE_DELIMITER: "拼接分隔符",
    ROLE_MERGE_SEPARATOR: "合并分隔符",
    ROLE_CLEAN_WHITESPACE: "清理空白字符",
    ROLE_MODEL_NAME: "底模（Checkpoint）",
    ROLE_LORA_NAME: "LoRA 名称",
    ROLE_LORA_STRENGTH: "LoRA 强度",
    ROLE_LORA_STRENGTH_CLIP: "LoRA 文本强度",
    ROLE_CLIP_NAME: "文本编码器（CLIP）",
    ROLE_VAE_NAME: "VAE",
    ROLE_DENOISE: "重绘强度",
    ROLE_SHIFT: "Flow Shift",
    ROLE_TEACACHE_THRESHOLD: "加速缓存阈值",
    ROLE_TEACACHE_ADAPTIVE: "自适应加速",
    ROLE_SAGE_ATTENTION: "SageAttention",
    ROLE_FILENAME_PREFIX: "输出文件名前缀",
    ROLE_TEXT_CONST: "常量文本",
    ROLE_FIXED: "固定值",
    ROLE_IGNORE: "忽略",
}

# 允许映射的字段值类型（其余如对象/连线一律跳过）
_SCALAR_TYPES = (str, int, float, bool)

# build 阶段：可从用户 --参数 覆盖的数值型用途
_INT_ROLES = {ROLE_WIDTH, ROLE_HEIGHT, ROLE_STEPS, ROLE_COUNT}
_FLOAT_ROLES = {
    ROLE_CFG,
    ROLE_ARTIST_STYLE_WEIGHT,
    ROLE_GELBOORU_STYLE_WEIGHT,
    ROLE_LORA_STRENGTH,
    ROLE_LORA_STRENGTH_CLIP,
    ROLE_DENOISE,
    ROLE_SHIFT,
    ROLE_TEACACHE_THRESHOLD,
}
_TEXT_ROLES = {
    ROLE_SAMPLER,
    ROLE_SCHEDULER,
    ROLE_ARTIST_STYLE,
    ROLE_GELBOORU_STYLE,
    ROLE_POSITIVE_PREFIX,
    ROLE_POSITIVE_SUFFIX,
    ROLE_PROMPT_PART_3,
    ROLE_PROMPT_PART_4,
    ROLE_PROMPT_PART_5,
    ROLE_PROMPT_PART_6,
    ROLE_DELIMITER,
    ROLE_MERGE_SEPARATOR,
    ROLE_MODEL_NAME,
    ROLE_LORA_NAME,
    ROLE_CLIP_NAME,
    ROLE_VAE_NAME,
    ROLE_SAGE_ATTENTION,
    ROLE_FILENAME_PREFIX,
    # 条件分支节点的文本（“横版 / 竖版”“真实标签”这类开关值），允许用 --参数 覆盖
    ROLE_TEXT_CONST,
}
_BOOL_ROLES = {
    ROLE_ARTIST_PREFIX,
    ROLE_TEACACHE_ADAPTIVE,
    ROLE_CLEAN_WHITESPACE,
}


def role_meta() -> dict[str, dict[str, str]]:
    """返回各“用途键”的中文标签与控件类型，供 WebUI 从模型调取参数时渲染。

    类型取值：int / float / bool / text / image；忽略项（ignore）不返回。
    前端据此为每个参数渲染合适的输入控件（数字框 / 开关 / 文本框）。
    """
    types: dict[str, str] = {}
    for role in _INT_ROLES:
        types[role] = "int"
    for role in _FLOAT_ROLES:
        types[role] = "float"
    for role in _BOOL_ROLES:
        types[role] = "bool"
    for role in _TEXT_ROLES:
        types[role] = "text"
    types[ROLE_SEED] = "int"
    types[ROLE_COUNT] = "int"
    types[ROLE_IMAGE] = "image"
    types[ROLE_PROMPT] = "text"
    result: dict[str, dict[str, str]] = {}
    for role, label in ROLE_LABELS.items():
        if role == ROLE_IGNORE:
            continue
        result[role] = {"label": label, "type": types.get(role, "text")}
    return result

# “单值”用途：同一个工作流里只应映射到一个节点字段
_MERGE_MIN_RATIO = 0.3  # 重传工作流时，新旧字段重合率低于这个值就判定为「换了一份工作流」
_SINGLE_USE_ROLES = {
    ROLE_PROMPT, ROLE_NEGATIVE, ROLE_WIDTH, ROLE_HEIGHT, ROLE_SEED, ROLE_COUNT, ROLE_IMAGE,
    ROLE_ARTIST_STYLE, ROLE_ARTIST_STYLE_WEIGHT, ROLE_ARTIST_PREFIX,
    ROLE_GELBOORU_STYLE, ROLE_GELBOORU_STYLE_WEIGHT,
    ROLE_POSITIVE_PREFIX, ROLE_POSITIVE_SUFFIX,
    ROLE_PROMPT_PART_3, ROLE_PROMPT_PART_4, ROLE_PROMPT_PART_5, ROLE_PROMPT_PART_6,
    ROLE_DELIMITER, ROLE_MERGE_SEPARATOR, ROLE_CLEAN_WHITESPACE,
    ROLE_MODEL_NAME, ROLE_LORA_NAME, ROLE_LORA_STRENGTH, ROLE_LORA_STRENGTH_CLIP,
    ROLE_CLIP_NAME, ROLE_VAE_NAME, ROLE_DENOISE, ROLE_SHIFT,
    ROLE_TEACACHE_THRESHOLD, ROLE_TEACACHE_ADAPTIVE, ROLE_SAGE_ATTENTION,
    ROLE_FILENAME_PREFIX, ROLE_TEXT_CONST,
}
# 条件分支 / 常量文本允许出现多次（「尺寸」和「版本」各一个），不做单值去重
_TEXT_CONST_CLASSES = (
    # 只认真正“装常量”的节点；CR Prompt Text 是提示词节点，不能进这里
    "cr text", "cr value", "cr combo", "primitive", "constant",
)
_TEXT_CONST_FIELDS = ("text", "value", "string", "text_g", "text_l", "prompt")

# 中文/常用别名 → 用途键（仅用于 ComfyUI 工作流模板的 --参数）
ROLE_ALIASES = {
    "画师": ROLE_ARTIST_STYLE,
    "画师风格": ROLE_ARTIST_STYLE,
    "画师风格权重": ROLE_ARTIST_STYLE_WEIGHT,
    "风格": ROLE_GELBOORU_STYLE,
    "画风": ROLE_GELBOORU_STYLE,
    "gelbooru": ROLE_GELBOORU_STYLE,
    "前缀": ROLE_POSITIVE_PREFIX,
    "正向前缀": ROLE_POSITIVE_PREFIX,
    "后缀": ROLE_POSITIVE_SUFFIX,
    "正向后缀": ROLE_POSITIVE_SUFFIX,
    "底模": ROLE_MODEL_NAME,
    "checkpoint": ROLE_MODEL_NAME,
    "lora": ROLE_LORA_NAME,
    "lora名称": ROLE_LORA_NAME,
    "lora强度": ROLE_LORA_STRENGTH,
    "clip": ROLE_CLIP_NAME,
    "文本编码器": ROLE_CLIP_NAME,
    "vae": ROLE_VAE_NAME,
    "重绘": ROLE_DENOISE,
    "重绘强度": ROLE_DENOISE,
    "shift": ROLE_SHIFT,
    "加速": ROLE_TEACACHE_THRESHOLD,
    "输出文件名": ROLE_FILENAME_PREFIX,
}


def normalize_role_aliases(params: Any) -> dict:
    """把 --参数 里的中文/常用别名归一为用途键，其余键原样保留。"""
    out: dict = {}
    for key, value in (params or {}).items():
        out[ROLE_ALIASES.get(str(key), str(key))] = value
    return out


def _value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "str"


def _guess_role(class_type: str, field_name: str, title: str) -> str:
    """按字段名/节点标题/节点类型猜测用途；猜不准返回 ignore，由用户下拉决定。"""
    fn = str(field_name or "").lower()
    tt = str(title or "").strip().lower()
    ct = str(class_type or "").lower()
    hay = f"{fn} {tt}"

    # 自定义节点常把尺寸/数量/负面做成字段名统一的常量节点（value），靠标题区分
    if tt in ("宽", "宽度") or "width" in tt:
        return ROLE_WIDTH
    if tt in ("高", "高度") or "height" in tt:
        return ROLE_HEIGHT
    if tt in ("数量", "批次", "batch") or "batch" in tt or "count" in tt:
        return ROLE_COUNT
    if "负面" in tt or "反向" in tt or "negative" in tt:
        return ROLE_NEGATIVE
    if ("正面" in tt or "positive" in tt or "prompt" in tt or "提示" in tt) and fn in (
        "text", "prompt", "value", "string",
    ):
        return ROLE_PROMPT

    if fn in ("width", "resolution_width"):
        return ROLE_WIDTH
    if fn in ("height", "resolution_height"):
        return ROLE_HEIGHT
    if "noise_seed" in fn or fn == "seed" or fn.endswith("_seed"):
        return ROLE_SEED
    if fn in ("steps", "step", "num_steps", "sample_steps", "sampling_steps") or fn.endswith("_steps"):
        return ROLE_STEPS
    if fn in ("cfg", "cfg_scale", "guidance", "guidance_scale"):
        return ROLE_CFG
    if fn in ("sampler_name", "sampler"):
        return ROLE_SAMPLER
    if fn == "scheduler":
        return ROLE_SCHEDULER
    if fn in ("batch_size", "batch", "count"):
        return ROLE_COUNT
    if fn == "negative":
        return ROLE_NEGATIVE

    # —— Anima / 画风类自定义节点 ——
    if fn == "artist_tags":
        return ROLE_ARTIST_STYLE
    if fn == "gelbooru_tags":
        return ROLE_GELBOORU_STYLE
    if fn == "use_artist_prefix":
        return ROLE_ARTIST_PREFIX
    if fn in ("last_picked", "gelbooru_tags_picked"):
        return ROLE_IGNORE
    if fn == "default_weight":
        if "artist" in ct:
            return ROLE_ARTIST_STYLE_WEIGHT
        if "gelbooru" in ct:
            return ROLE_GELBOORU_STYLE_WEIGHT
        return ROLE_IGNORE
    if fn in ("part_3", "part_4", "part_5", "part_6") and "combiner" in ct:
        return "prompt_" + fn

    # —— 模型 / LoRA / 采样器高级参数 ——
    if fn == "model_name" or fn in ("ckpt_name", "unet_name") or "checkpointloader" in ct:
        return ROLE_MODEL_NAME
    if fn == "lora_name" or (ct.startswith("lora") and "loader" in ct):
        return ROLE_LORA_NAME
    if fn == "strength_model":
        return ROLE_LORA_STRENGTH
    if fn == "strength_clip":
        return ROLE_LORA_STRENGTH_CLIP
    if fn in ("clip_name", "clip_name1", "clip_name2"):
        return ROLE_CLIP_NAME
    if fn == "vae_name":
        return ROLE_VAE_NAME
    if fn == "denoise":
        return ROLE_DENOISE
    if fn == "shift":
        return ROLE_SHIFT
    if fn == "threshold" and "teacache" in ct:
        return ROLE_TEACACHE_THRESHOLD
    if fn == "adaptive_mode" and "teacache" in ct:
        return ROLE_TEACACHE_ADAPTIVE
    if fn == "sage_attention":
        return ROLE_SAGE_ATTENTION
    if fn == "filename_prefix":
        return ROLE_FILENAME_PREFIX
    # 条件分支的表达式（MathExpression 的 expression 等）：经常需要按尺寸联动改
    if fn in ("expression", "expr") or "mathexpression" in ct:
        return ROLE_TEXT_CONST
    if fn == "delimiter":
        return ROLE_DELIMITER
    if fn == "merge_separator":
        return ROLE_MERGE_SEPARATOR
    if fn == "clean_whitespace":
        return ROLE_CLEAN_WHITESPACE

    if ct == "cliptextencode" or fn in ("text", "prompt", "positive", "string", "tags"):
        # 纯常量节点（CR Text / CR Value / Primitive）的 text 不是提示词，
        # 否则会把真正的提示词节点挤掉，或者把「尺寸：横版」当成正文发出去
        if _node_has_text_const_field(class_type or "", {field_name: ""}) or any(
            marker in ct for marker in _TEXT_CONST_CLASSES
        ):
            return ROLE_IGNORE
        if any(word in hay for word in ("negative", "neg_", "负", "反向")):
            return ROLE_NEGATIVE
        return ROLE_PROMPT
    # LoadImage 一类的图片输入节点（图生图/局部重绘的参考图）。
    if fn == "image" and ("load" in ct or "inpaint" in ct or "image" in ct):
        return ROLE_IMAGE
    return ROLE_IGNORE


def _node_has_text_const_field(class_type: str, inputs: Any) -> dict[str, Any]:
    """返回该节点里应标成「常量文本」的字段；不适合则返回空 dict。

    只认纯常量/表达式节点（CR Text、CR Value、Primitive、MathExpression…），
    提示词节点（CR Prompt Text、CLIPTextEncode）一律不算，避免把提示词节点
    误标成常量文本。
    """
    ct = str(class_type or "").strip().lower()
    if "combiner" in ct:  # 拼接节点有专门的分区用途
        return {}
    if not isinstance(inputs, dict):
        return {}
    is_const_class = any(marker in ct for marker in _TEXT_CONST_CLASSES)
    is_expr_class = "mathexpression" in ct or "expression" in ct
    if not (is_const_class or is_expr_class):
        return {}
    picked: dict[str, Any] = {}
    for field_name, value in inputs.items():
        name = str(field_name).strip().lower()
        if name not in _TEXT_CONST_FIELDS and name not in ("expression", "expr"):
            continue
        if not isinstance(value, _SCALAR_TYPES) or isinstance(value, bool):
            continue
        picked[str(field_name)] = value
    return picked


def _referenced_node_ids(workflow: Any) -> set[str]:
    """收集所有被连线引用过的节点 ID（``["900", 0]`` 的第一项）。"""
    refs: set[str] = set()
    if not isinstance(workflow, dict):
        return refs
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for value in inputs.values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int)):
                refs.add(str(value[0]))
    return refs


def analyze_workflow(workflow: Any) -> list[dict[str, Any]]:
    """解析 API 格式工作流，返回可设置字段列表。

    同一个“单值”用途（提示词/负面/宽/高/种子等）只保留第一个命中的字段，
    其余同用途字段降级为 ignore，避免一次请求把同一用途写到多个节点上；
    用户仍可在下拉里手动调整。
    """
    if not isinstance(workflow, dict):
        raise ValueError("工作流 JSON 顶层必须是对象（API 格式导出）")
    single_use = _SINGLE_USE_ROLES
    # 被下游连线引用的节点：这类节点里的常量文本才是“总开关”（如尺寸分支），
    # 没有被引用的常量节点多半只是备用的提示词草稿，不去打扰它
    referenced = _referenced_node_ids(workflow)
    seen: set[str] = set()
    fields: list[dict[str, Any]] = []
    # (用途, 字段名) → 出现的节点个数；多个节点共用同一用途时，后面要加节点前缀区分
    role_field_nodes: dict[tuple[str, str], list[str]] = {}
    for node_id, node in workflow.items():
        if not isinstance(node, dict) or "class_type" not in node:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
        title = str(meta.get("title") or "").strip()
        class_type = str(node.get("class_type") or "").strip()
        const_fields = (
            _node_has_text_const_field(class_type, inputs) if str(node_id) in referenced else {}
        )
        for field_name, value in inputs.items():
            # 被连线引用的值是 list（节点链接），API 不能直接改；对象同理跳过
            if not isinstance(value, _SCALAR_TYPES):
                continue
            role = _guess_role(class_type, field_name, title)
            if role == ROLE_IGNORE and field_name in const_fields:
                role = ROLE_TEXT_CONST
            if role in single_use and role in seen:
                role = ROLE_IGNORE
            elif role in single_use:
                seen.add(role)
            fields.append({
                "nodeId": str(node_id),
                "fieldName": str(field_name),
                "value": value,
                "valueType": _value_type(value),
                "classType": class_type,
                "title": title,
                "role": role,
            })
            if role != ROLE_IGNORE:
                role_field_nodes.setdefault((role, str(field_name)), []).append(str(node_id))

    # 同一用途被多个节点共用时，WebUI 的下拉按用途去重会把它们折叠成一个。
    # 这里给重复的常量文本补上「节点号 · 标题」前缀，保证下拉里能看到每一项。
    for field in fields:
        role = field["role"]
        if role != ROLE_TEXT_CONST:
            continue
        ids = role_field_nodes.get((role, field["fieldName"])) or []
        if len(ids) <= 1:
            continue
        label = field["title"] or field["classType"] or f"节点 {field['nodeId']}"
        field["fieldName"] = f"{field['nodeId']} · {label}"
    return fields


def merge_mapping(old_nodes: Any, new_nodes: Any) -> dict[str, Any]:
    """把旧映射表里用户调过的用途搬到新解析出的字段上，避免重新上传就把映射清空。

    匹配键是 ``(nodeId, fieldName, classType)`` 三元组：节点 ID 与字段名都对上、且节点
    类型没变，才认为「同一个参数」，沿用旧的 ``role``。``value`` 一律以新 JSON 为准
    （JSON 是工作流的真相，模型/LoRA 文件名这类更新必须能生效），差异只做统计提示。

    重合率低于 ``_MERGE_MIN_RATIO`` 视为换了一份工作流，直接重建映射，避免错位映射。

    返回 ``{"nodes": [...], "summary": {...}}``。
    """
    news = [node for node in (new_nodes or []) if isinstance(node, dict)]
    olds = [node for node in (old_nodes or []) if isinstance(node, dict)]
    total = len(news)
    if not olds or not total:
        return {"nodes": news, "summary": {"kept": 0, "added": total, "removed": 0, "rebuilt": bool(olds)}}

    old_index = {
        (str(node.get("nodeId")), str(node.get("fieldName")), str(node.get("classType"))): node
        for node in olds
    }
    matched: set[tuple[str, str, str]] = set()
    kept = 0
    added = 0
    changed_values = 0
    for node in news:
        key = (str(node.get("nodeId")), str(node.get("fieldName")), str(node.get("classType")))
        previous = old_index.get(key)
        if previous is None:
            added += 1
            continue
        matched.add(key)
        role = str(previous.get("role") or ROLE_IGNORE)
        if role != ROLE_IGNORE:
            node["role"] = role
            kept += 1
        if str(previous.get("value")) != str(node.get("value")):
            changed_values += 1

    overlap = len(matched) / total if total else 0.0
    if overlap < _MERGE_MIN_RATIO:
        fresh = [dict(node) for node in news]
        return {
            "nodes": fresh,
            "summary": {
                "kept": 0, "added": total, "removed": len(olds),
                "rebuilt": True, "overlap": round(overlap, 3),
            },
        }

    removed = len([key for key in old_index if key not in matched])
    return {
        "nodes": news,
        "summary": {
            "kept": kept,
            "added": added,
            "removed": removed,
            "rebuilt": False,
            "value_changed": changed_values,
            "overlap": round(overlap, 3),
        },
    }


def upgrade_workflow_roles(nodes: Any, workflow: Any = None) -> bool:
    """给已保存的映射表补用途：把 role=ignore 且能被猜出的字段升级成对应用途。

    仅用于兼容旧配置（新增用途后无需重新上传工作流 JSON）。幂等：已经映射过的
    用途不会被改动，单值用途也不会重复映射。

    ``workflow`` 传入原始工作流 JSON 时会额外做一件事：把“被下游连线引用的常量
    节点”（如条件分支里的「尺寸：横版/竖版」）标成常量文本。这类节点的字段名
    往往是 ``text``，直接按名字猜会被误升成提示词，所以只按节点类型判断。

    返回是否发生了改动。
    """
    if not isinstance(nodes, list):
        return False
    used = {
        str(node.get("role"))
        for node in nodes
        if isinstance(node, dict) and str(node.get("role") or ROLE_IGNORE) != ROLE_IGNORE
    }
    refs = _referenced_node_ids(workflow)
    # 按节点分组：常量文本以“节点”为单位判断，避免半截升级
    per_node: dict[str, list[dict]] = {}
    for node in nodes:
        if isinstance(node, dict):
            per_node.setdefault(str(node.get("nodeId")), []).append(node)

    changed = False
    const_nodes: set[str] = set()
    if refs:
        for node_id, group in per_node.items():
            sample = group[0]
            inputs = {str(item.get("fieldName")): item.get("value") for item in group}
            picked = _node_has_text_const_field(sample.get("classType", ""), inputs)
            if not picked:
                continue
            if node_id not in refs:
                continue  # 没被任何节点引用，多半只是摆着看的，不硬改
            const_nodes.add(node_id)
            for item in group:
                if item.get("fieldName") in picked and str(item.get("role") or ROLE_IGNORE) in (
                    ROLE_IGNORE, ROLE_PROMPT,
                ):
                    item["role"] = ROLE_TEXT_CONST
                    changed = True

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if str(node.get("role") or ROLE_IGNORE) != ROLE_IGNORE:
            continue
        if str(node.get("nodeId")) in const_nodes:
            continue
        role = _guess_role(node.get("classType", ""), node.get("fieldName", ""), node.get("title", ""))
        if role == ROLE_IGNORE:
            continue
        if role in _SINGLE_USE_ROLES and role in used:
            continue
        node["role"] = role
        if role in _SINGLE_USE_ROLES:
            used.add(role)
        changed = True
    return changed


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on", "开", "是"):
            return True
        if low in ("false", "0", "no", "off", "关", "否"):
            return False
    return default


def _resolve_value(role: str, node: dict, payload: dict, refer_field: str = "") -> Any:
    """决定某个映射字段本次要写入的值。payload 为用户提示词与 --参数。"""
    stored = node.get("value")
    if role == ROLE_IMAGE:
        # 参考图：优先取模板 refer_field（build_payload 已写入 payload），
        # 再兜底常见图片字段名；拿到 data URI / URL 后写入 LoadImage 节点。
        value = payload.get(refer_field) if refer_field else None
        if not value:
            for key in ("image", "images", "init_image", "input_image"):
                if payload.get(key):
                    value = payload[key]
                    break
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return value if value not in (None, "") else stored
    if role == ROLE_PROMPT:
        return str(payload.get("prompt") or "")
    if role == ROLE_NEGATIVE:
        return str(payload.get("negative") or stored or "")
    if role == ROLE_SEED:
        if "seed" in payload:
            return _coerce_int(payload.get("seed"), 0)
        # RunningHub 提交时会强制重置种子，这里每次都显式给一个随机值
        return random.randint(0, 2**31 - 1)
    if role in _INT_ROLES:
        if role in payload:
            return _coerce_int(payload.get(role), _coerce_int(stored, 0))
        return _coerce_int(stored, 0)
    if role in _FLOAT_ROLES:
        if role in payload:
            return _coerce_float(payload.get(role), _coerce_float(stored, 0.0))
        return _coerce_float(stored, 0.0)
    if role in _TEXT_ROLES:
        return str(payload.get(role) or stored or "")
    if role in _BOOL_ROLES:
        if role in payload:
            return _coerce_bool(payload.get(role), _coerce_bool(stored, False))
        return _coerce_bool(stored, False)
    # fixed 及其它：直接使用工作流原值
    return stored


def build_node_info_list(nodes: Any, payload: dict, refer_field: str = "") -> list[dict[str, Any]]:
    """把映射表 + 本次 payload 拼成 RunningHub nodeInfoList。"""
    result: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        role = str(node.get("role") or ROLE_IGNORE)
        if role == ROLE_IGNORE:
            continue
        node_id = str(node.get("nodeId") or "").strip()
        field_name = str(node.get("fieldName") or "").strip()
        if not node_id or not field_name:
            continue
        result.append({
            "nodeId": node_id,
            "fieldName": field_name,
            "fieldValue": _resolve_value(role, node, payload or {}, refer_field),
        })
    return result
