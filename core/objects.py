# -*- coding: utf-8 -*-
"""领域对象与纯函数：历史快照、发送决策、清理工具。

这些函数**不碰网络、不碰框架**，只做纯计算，因此可以放心单元测试。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# APNG 三态："" 跟随全局 / "on" 强制开 / "off" 强制关
APNG_FOLLOW = ""
APNG_ON = "on"
APNG_OFF = "off"

# 拆解结局
DECOMPOSE_SUCCESS = "success"
DECOMPOSE_FALLBACK = "fallback"
DECOMPOSE_DISABLED = "disabled"


@dataclass
class PendingGeneration:
    """已提交但排队未完成的任务（超时后由事件层转入后台补发）。"""

    client: object
    provider: str
    model: str
    task_id: str
    key: Optional[str] = None


@dataclass
class GenerationResult:  # 仅兼容旧代码；主流程实际用 core.handler.HandlerResult
    """一次生图请求的结果，同时承载写历史所需的元数据。

    注意：真正的生图结果类型是 :class:`core.handler.HandlerResult`，
    本类只为外部旧调用方保留，不要在插件内部新代码里使用。
    """

    images: list[str] = field(default_factory=list)
    text: str = ""
    silent: bool = False
    trigger: str = ""
    model_template: str = ""
    provider: str = ""
    model: str = ""
    params: dict = field(default_factory=dict)
    refer_image_count: int = 0
    task_id: str = ""
    resolved_prompt: str = ""
    umo: str = ""
    pending: Optional[PendingGeneration] = None
    # 命中预设时，该预设的 APNG 三态（"" 跟随全局 / "on" / "off"）
    apng: str = ""
    preset_trigger: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.images)


@dataclass
class RequestContext:
    """一次请求的“生效选项快照”，供历史记录与后台补发复用。"""

    trigger: str = ""
    apng_choice: str = ""
    decompose_requested: bool = False
    decompose_status: str = DECOMPOSE_DISABLED
    decompose_error: str = ""
    tags: str = ""
    apng_enabled: Optional[bool] = None
    resolved_prompt: str = ""
    params: dict = field(default_factory=dict)
    model: str = ""
    refer_image_count: int = 0
    task_id: str = ""
    umo: str = ""
    # 用户自己发的那几张图（插件落地目录下的临时副本），后台补发时用它清理
    source_images: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "apng_choice": self.apng_choice,
            "decompose_requested": self.decompose_requested,
            "decompose_status": self.decompose_status,
            "decompose_error": self.decompose_error,
            "tags": self.tags,
            "apng_enabled": self.apng_enabled,
            "resolved_prompt": self.resolved_prompt,
            "params": dict(self.params or {}),
            "model": self.model,
            "refer_image_count": self.refer_image_count,
            "task_id": self.task_id,
            "umo": self.umo,
            "source_images": list(self.source_images or []),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "RequestContext":
        """从（可能来自旧版本的）快照恢复，缺字段时按 tags 推断拆解结局。"""
        data = data or {}
        ctx = cls(
            trigger=str(data.get("trigger", "") or ""),
            apng_choice=str(data.get("apng_choice", "") or "").strip().lower(),
            decompose_requested=bool(data.get("decompose_requested")),
            decompose_status=str(data.get("decompose_status", "") or ""),
            decompose_error=str(data.get("decompose_error", "") or ""),
            tags=str(data.get("tags", "") or ""),
            resolved_prompt=str(data.get("resolved_prompt", "") or ""),
            params=dict(data.get("params") or {}) if isinstance(data.get("params"), dict) else {},
            model=str(data.get("model", "") or ""),
            refer_image_count=_to_int(data.get("refer_image_count")),
            task_id=str(data.get("task_id", "") or ""),
            umo=str(data.get("umo", "") or ""),
            source_images=[
                str(item) for item in (data.get("source_images") or []) if str(item).strip()
            ] if isinstance(data.get("source_images"), (list, tuple)) else [],
        )
        if not ctx.decompose_status:
            ctx.decompose_status = (
                DECOMPOSE_SUCCESS if ctx.tags
                else (DECOMPOSE_FALLBACK if ctx.decompose_requested else DECOMPOSE_DISABLED)
            )
        ctx.apng_enabled = apng_enabled_from_choice(ctx.apng_choice)
        return ctx

    def history_options(self, **extra) -> dict:
        """拼装写入历史库的选项快照。

        **不包含** ``source_images``：临时副本路径只在本次请求内有效，
        写进历史库没有意义（历史里存的是 source_image_paths 那几份归档）。
        """
        options = {
            "decomposed": bool(self.decompose_status == DECOMPOSE_SUCCESS or self.tags),
            "decompose_requested": bool(self.decompose_requested),
            "decompose_status": self.decompose_status,
            "decompose_error": self.decompose_error,
            "tags": self.tags,
            "apng": self.apng_choice,
            "apng_enabled": self.apng_enabled,
        }
        params = self.params if isinstance(self.params, dict) else {}
        if params:
            options["params"] = {
                str(k): v for k, v in params.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
        refer = int(self.refer_image_count or 0)
        if refer:
            options["refer_images"] = refer
        if self.umo:
            options["umo"] = self.umo
        options.update(extra)
        return options


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def apng_enabled_from_choice(choice: Any) -> Optional[bool]:
    """把预设的 APNG 三态转成 ``_wrap_apng`` 需要的形式（None = 跟随全局）。"""
    value = str(choice or "").strip().lower()
    if value == APNG_ON:
        return True
    if value == APNG_OFF:
        return False
    return None


def resolve_apng_choice(*candidates: Any) -> str:
    """按“先到先用”挑出本次生效的 APNG 三态。

    生图流程里有几个可能的来源：生成结果上带的（命中预设时由 handler 透传）、
    预设表本身，以及历史记录里恢复出来的快照。以前每处各写一遍 ``a or b``，
    其中一处把 ``""`` 当成“没设置”而回落到 ``result.apng``，导致两处结论不一致。
    统一成一条规则：第一个非空的候选值生效。
    """
    for value in candidates:
        text = str(value or "").strip().lower()
        if text:
            return text
    return ""


def build_request_context(
    result: GenerationResult,
    *,
    trigger: str = "",
    apng_choice: str = "",
    decompose_requested: bool = False,
    decompose_status: str = DECOMPOSE_DISABLED,
    decompose_error: str = "",
    tags: str = "",
    umo: str = "",
) -> RequestContext:
    """从生成结果 + 本次准备阶段的状态，汇总成快照。"""
    return RequestContext(
        trigger=trigger or getattr(result, "trigger", "") or "",
        apng_choice=str(apng_choice or "").strip().lower(),
        decompose_requested=bool(decompose_requested),
        decompose_status=str(decompose_status or DECOMPOSE_DISABLED),
        decompose_error=str(decompose_error or ""),
        tags=str(tags or ""),
        apng_enabled=apng_enabled_from_choice(apng_choice),
        resolved_prompt=str(getattr(result, "resolved_prompt", "") or ""),
        params=dict(getattr(result, "params", {}) or {}),
        model=str(getattr(result, "model", "") or ""),
        refer_image_count=_to_int(getattr(result, "refer_image_count", 0)),
        task_id=str(getattr(result, "task_id", "") or ""),
        umo=str(umo or getattr(result, "umo", "") or ""),
    )


# ---------------------------------------------------------------------- 清理
def cleanup_transient_apngs(sent_paths: list[str], originals: list[str]) -> None:
    """删除本次临时生成的 APNG 包装文件（历史记录始终指向原始生成图）。

    包装文件可能是 .png / .webp / .gif，不能只看扩展名；判据是“不在原始图集合里”。
    """
    original_set = {str(Path(p)) for p in originals or []}
    for path in sent_paths or []:
        value = str(path)
        if not value or str(Path(value)) in original_set:
            continue
        try:
            candidate = Path(value)
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        except (OSError, ValueError):
            continue


def remove_files(paths: list[str]) -> None:
    """尽量删除一批临时文件；失败不抛异常。"""
    for path in paths or []:
        if not path:
            continue
        try:
            Path(str(path)).unlink(missing_ok=True)
        except (OSError, ValueError):
            continue


def event_umo(event: Any) -> str:
    return str(getattr(event, "unified_msg_origin", "") or "")
