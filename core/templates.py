# -*- coding: utf-8 -*-
"""模型模板管理器（完全模板化：模型名/提交参数/参考图字段均由用户配置）。

模板负责描述提供商、模型和请求参数：
- 每个模板 = 一个 WaveSpeed 模型（模型名拼 URL）
- 模板的 params 为提交参数（用户可完全自定义键值）
- refer_field 为参考图字段名（图生图模型如 images；文生图留空）
- 提示词中的 --参数 会透传覆盖模板参数同名键，并可新增任意键
"""
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

_NUM_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")

def derive_provider(raw):
    """模型模板按名称选择用户配置的模型提供商。"""
    return str(raw.get("provider") or "").strip()


@dataclass
class ModelTemplate:
    name: str                       # 模板名（--model 引用）
    model: str                      # 模型名（拼 URL）
    provider: str = ""              # 模型提供商名称
    enabled: bool = True
    enabled_as_default: bool = True
    fallback_order: int = 0
    refer_field: str = ""           # 参考图字段名，如 images；空表示不支持参考图
    max_refer_images: int = 10
    min_prompt_length: int = 0      # 提示词最小长度（0 表示不限制）
    params: dict = field(default_factory=dict)   # 提交参数（用户自定义）


def normalize_param(value: Any) -> Any:
    """把提示词参数值规范化为 JSON 友好类型（数字/bool/字符串）。"""
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none"):
        return None
    if _NUM_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)
    return s


def _parse_params(raw: Any) -> dict:
    """兼容 object 与 JSON 字符串两种配置形态。"""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items()}
        except json.JSONDecodeError:
            pass
    return {}


class TemplateManager:
    def __init__(self, raw_templates: list | None = None):
        self.templates: dict[str, ModelTemplate] = {}
        for raw in raw_templates or []:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            model = str(raw.get("model", "")).strip()
            if not name or not model:
                continue
            try:
                max_ref = max(0, int(raw.get("max_refer_images", 10)))
            except (TypeError, ValueError):
                max_ref = 10
            try:
                min_pl = max(0, int(raw.get("min_prompt_length", 0)))
            except (TypeError, ValueError):
                min_pl = 0
            self.templates[name] = ModelTemplate(
                name=name,
                model=model,
                provider=derive_provider(raw),
                enabled=bool(raw.get("enabled", True)),
                enabled_as_default=bool(raw.get("enabled_as_default", True)),
                fallback_order=int(raw.get("fallback_order", 0) or 0),
                refer_field=str(raw.get("refer_field", "")).strip(),
                max_refer_images=max_ref,
                min_prompt_length=min_pl,
                params=_parse_params(raw.get("params")),
            )

    def get(self, name: str) -> Optional[ModelTemplate]:
        return self.templates.get(name)

    def names(self) -> list[str]:
        return list(self.templates)

    def enabled_names(self) -> list[str]:
        return [n for n, t in self.templates.items() if t.enabled]

    def default_candidates(self, has_images: bool) -> list[ModelTemplate]:
        """按数字从小到大返回允许充当默认值且模式匹配的模板。"""
        candidates = [
            template for template in self.templates.values()
            if template.enabled
            and template.enabled_as_default
            and bool(template.refer_field) is has_images
        ]
        return sorted(candidates, key=lambda template: template.fallback_order)

    def fallback(self, has_images: bool) -> Optional[ModelTemplate]:
        candidates = self.default_candidates(has_images)
        return candidates[0] if candidates else None

    def resolve(self, name: str) -> Optional[ModelTemplate]:
        """按名称解析模板（仅启用）。"""
        t = self.templates.get(name)
        return t if t is not None and t.enabled else None


def build_payload(
    prompt: str,
    user_params: dict,
    template: ModelTemplate,
    refer_uris: list[str] | None = None,
) -> dict:
    """构造提交 payload。

    - 基础：模板 params（值规范化）
    - 覆盖：提示词 --参数 同名覆盖、新键透传（model 键除外，它是模型选择用途）
    - 参考图：按模板 refer_field 字段名注入
    """
    payload: dict = {k: normalize_param(v) for k, v in template.params.items()}
    for key, value in (user_params or {}).items():
        if key == "model":
            continue
        payload[key] = normalize_param(value)
    payload["prompt"] = prompt
    if refer_uris and template.refer_field:
        # 主流兼容接口的 image/init_image/input_image 是单图字符串；
        # images 等复数字段保留数组。模型配置仍可自行改成其他字段。
        scalar_fields = {"image", "init_image", "input_image"}
        payload[template.refer_field] = (
            refer_uris[0] if template.refer_field.lower() in scalar_fields else refer_uris
        )
    return payload
