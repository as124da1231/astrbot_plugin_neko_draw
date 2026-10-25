# -*- coding: utf-8 -*-
"""扩写 / 提取形象：独立于生图的一条文字处理线。

命中触发词 → 按需取图 → 调对话模型 → 返回纯文本。
全程不提交生图任务、不占生图额度，也不会误入生图流程。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from astrbot.api import logger

from .command_presets import section_spec
from .config_store import get_bool, get_int
from .image_collector import ImageCollector
from .llm_gateway import LlmGateway
from .objects import remove_files
from .parser import strip_at_mentions
from .text_utils import detect_tag_refusal, scene_message


# 调用来源：用户主动发送的指令 / 生图流程内部的自动预处理
SOURCE_USER = "user"
SOURCE_BOT = "bot"


# 区分“没命中该分区”和“命中了但结果为空”：命中时返回 TextToolResult，未命中返回 None
@dataclass
class TextToolResult:
    """一次扩写/提取的执行结果。

    ``text`` 是准备发出去的文本（含结束提示、失败文案）；
    ``raw`` 是模型原样返回的内容，供生图预处理直接取用。
    """

    text: str = ""
    raw: str = ""
    source: str = SOURCE_USER
    section: str = ""
    trigger: str = ""
    image_count: int = 0
    image_source: str = ""
    model: str = ""
    duration_ms: float = 0.0
    template: str = ""
    body: str = ""
    system_prompt: str = ""
    reason: str = ""
    meta: dict = field(default_factory=dict)


class TextToolService:
    """扩写与提取形象两个分区的业务实现。"""

    def __init__(
        self,
        *,
        conf: Any,
        gateway: LlmGateway,
        collector: ImageCollector,
        store_getter,
        history_recorder=None,
        overrides=None,
    ):
        """
        ``store_getter``：返回 ``{"expand": store, "extract": store}`` 的回调，
        这样配置重载后自动拿到新的存储实例。
        """
        self.conf = conf
        self.gateway = gateway
        self.collector = collector
        self.store_getter = store_getter
        self.history_recorder = history_recorder
        self.overrides = overrides if overrides is not None else {}
        # 供兼容层使用的引用（main.prompt_manager 被替换时由 App 同步过来）
        self.prompt_manager = None

    def store(self, section: str):
        """当前生效的扩写/提取存储（配置重载后会返回新实例）。"""
        stores = self.store_getter() or {}
        if not isinstance(stores, dict):
            return None
        return stores.get(section)

    # ------------------------------------------------------------------ 旧接口
    def _should_decompose(self, preset=None) -> bool:
        """旧名字，转发到「生图前是否预处理」。"""
        return self.should_preprocess(preset)

    async def _decompose_tags(self, user_text, event=None, preset=None) -> tuple[str, str]:
        """旧名字，转发到用扩写条目做预处理。"""
        return await self.preprocess(user_text, event, preset)

    # ------------------------------------------------------------------ 生图预处理
    def should_preprocess(self, preset: Optional[dict] = None) -> bool:
        """生图前是否先把用户原文交给扩写条目处理。

        优先级：预设「预处理」= on/off 直接决定；预设留空则跟随全局总开关。
        """
        choice = str((preset or {}).get("decompose", "") or "").strip().lower()
        if choice == "on":
            return True
        if choice == "off":
            return False
        return get_bool(self.conf, "llm_tag_enable", False)

    def _preprocess_item(self, preset: Optional[dict] = None) -> Optional[dict]:
        """挑出本次预处理要用的扩写条目（预设指定 > 第一条启用的）。"""
        store = self.store("expand")
        if store is None:
            return None
        chosen = str((preset or {}).get("tag_prompt", "") or "").strip()
        if chosen:
            return store.get_by_trigger(chosen)
        for candidate in store.list_items():
            if candidate.get("enabled", True) is not False:
                return candidate
        return None

    async def preprocess(
        self, user_text: str, event: Any, preset: Optional[dict] = None
    ) -> tuple[str, str]:
        """生图前的文字预处理：用扩写条目把用户原话变成完整提示词。

        返回 ``(结果, 失败原因)``；失败时结果为空，由生图流程决定是否回退原文。
        与用户主动发送的「扩写」指令共用同一份模板与实现，只是来源标记不同。
        """
        text = str(user_text or "").strip()
        if not text:
            return "", "用户正文为空"
        store = self.store("expand")
        if store is None:
            return "", "扩写指令不可用"
        chosen = str((preset or {}).get("tag_prompt", "") or "").strip()
        item = store.get_by_trigger(chosen) if chosen else None
        if chosen and item is None:
            # 预设引用了一条后来被删掉的扩写：降级到默认条目。
            # 这里不能直接报错——预设是历史配置，删条目是日常操作，
            # 因为一次删条目就让所有人出图失败代价太大。
            logger.info(
                "[neko_draw] 预设引用的扩写指令「%s」已不存在，改用默认扩写条目", chosen
            )
        if item is None:
            item = self._preprocess_item({"tag_prompt": ""})
        if item is None:
            return "", "没有可用的扩写指令，请先在「文字加工 → 扩写指令编辑」里添加"
        if item.get("enabled", True) is False:
            return "", f"扩写指令「{item.get('trigger')}」已停用"
        outcome = await self._invoke(
            event, "expand", item, text,
            trigger=str(item.get("trigger") or ""),
            source=SOURCE_BOT, notify_end=False,
        )
        if not outcome.raw:
            return "", outcome.reason
        # 预处理结果会直接交给画图模型：像“抱歉，我不能……”这类说明必须先拦下来，
        # 否则会原样当成提示词提交，出来的必然是废图。
        refusal = detect_tag_refusal(outcome.raw)
        if refusal:
            logger.warning(
                "[neko_draw] 预处理结果被判定为%s，已丢弃（前 200 字：%s）",
                refusal, outcome.raw[:200],
            )
            return "", refusal
        return outcome.raw, ""

    # ------------------------------------------------------------------ LLM
    async def _command_llm(self, event: Any, **kwargs) -> tuple[str, str]:
        """调用对话模型（可被 ``app.overrides`` 或子类替换）。

        注意：先取出实现再 await，避免协程执行期间 overrides 被改写。
        """
        override = (self.overrides or {}).get("_command_llm")
        chat = override if override is not None else self.gateway.chat
        return await chat(event, **kwargs)

    # ------------------------------------------------------------------ 匹配
    def match(self, section: str, text: str) -> Optional[dict]:
        """命中该分区的触发词时返回条目（**含已停用的**），否则 None。

        停用与否由调用方判断：``run()`` 会跳过停用条目，列表/详情页则要展示它们。
        触发词匹配规则只保留在 ``CommandPresetStore.match`` 里一份。
        """
        store = self.store(section)
        return store.match(text) if store is not None else None

    # ------------------------------------------------------------------ 模板
    @staticmethod
    def _render_template(item: dict, body: str, section: str) -> str:
        template = str(item.get("prompt") or "")
        if "{{user_text}}" in template:
            return template.replace("{{user_text}}", body)
        if body and template.strip():
            return f"{template}\n{body}"
        if body:
            # 模板留空时也要把用户补充的说明（例如「只管发型」）带给模型
            return body
        if section == "extract" and template.rstrip().endswith("："):
            return template.rstrip() + "（无）"
        return template

    # ------------------------------------------------------------------ 取图
    async def _collect_images(self, event: Any) -> tuple[list[str], list[str]]:
        """按统一取图规则取图。

        覆盖项（兼容层替换的解析器）由 ``ImageCollector.overrides_from`` 统一挑，
        这里不再自己维护一份“该替换哪几个”的清单。
        """
        overrides = ImageCollector.overrides_from(self.overrides)
        result = await self.collector.collect(event, overrides=overrides)
        sources = [result.source] if result.source else []
        return list(result.paths), sources

    # ------------------------------------------------------------------ 主流程
    async def run(
        self, event: Any, section: str, text: str, item: Optional[dict] = None
    ) -> Optional[TextToolResult]:
        """执行一次用户主动发送的扩写/提取；未命中返回 None。"""
        if item is None:
            item = self.match(section, text)
        # 停用的条目仍会被 match 返回（列表/详情要展示它），但不该响应消息
        if item is None or item.get("enabled", True) is False:
            return None
        spec = section_spec(section)
        body = strip_at_mentions(text, self.collector.mention_texts(event))
        trigger = str(item.get("trigger") or "")
        if body.startswith(trigger):
            body = body[len(trigger):].strip()

        image_paths: list[str] = []
        sources: list[str] = []
        if spec["needs_image"]:
            image_paths, sources = await self._collect_images(event)
            if not image_paths:
                # 提示只说“现在真的可用的取图方式”，不再一律让用户去 @ 人。
                # 注意：这里返回的只是一个结果对象（不是“没命中”的 None），
                # 主流程会照常把它当文字回复发出去。
                return TextToolResult(
                    text=f"{spec['label']}需要图片：{self.collector.missing_hint()}",
                    section=section, trigger=trigger,
                )
        elif not body:
            return TextToolResult(
                text=f"用法：{trigger} <要扩写的内容>",
                section=section, trigger=trigger,
            )

        provider_id, model, _timeout = self._resolve_llm(section, item)
        await self._notify_start(event, section, trigger, model or provider_id)
        return await self._invoke(
            event, section, item, body,
            trigger=trigger, image_paths=image_paths, sources=sources,
            source=SOURCE_USER, notify_end=True,
        )

    def _resolve_isolation(self, section: str, item: dict) -> bool:
        """解析本次是否使用独立会话：条目自身 > 分区全局（expand/extract 各一个开关）。"""
        scene = "extract" if section == "extract" else "expand"
        choice = str((item or {}).get("isolation") or "").strip().lower()
        if choice == "on":
            return True
        if choice == "off":
            return False
        return get_bool(self.conf, f"{scene}_isolation", False)

    def _resolve_llm(self, section: str, item: dict) -> tuple[str, str, int]:
        """解析本次使用的提供商 / 模型 / 超时：条目自身 > 分区全局 > 会话默认。"""
        scene = "extract" if section == "extract" else "expand"
        default_timeout = 90 if scene == "extract" else 60
        try:
            fallback_timeout = int(get_int(self.conf, f"{scene}_global_timeout", default_timeout))
        except (TypeError, ValueError):
            fallback_timeout = default_timeout
        if fallback_timeout <= 0:
            fallback_timeout = default_timeout
        try:
            timeout = int(item.get("timeout") or 0) or max(1, fallback_timeout)
        except (TypeError, ValueError):
            timeout = max(1, fallback_timeout)
        provider_id = str(item.get("provider_id", "") or "").strip() or str(
            self.conf.get(f"{scene}_provider_id", "") or ""
        ).strip()
        # 条目里显式指定的模型优先于分区全局模型；是否真的可用交给网关按
        # 提供商的模型列表判断（不匹配会回退到提供商默认，不会硬套别家模型名）。
        item_model = str(item.get("model", "") or "").strip()
        global_model = str(self.conf.get(f"{scene}_model", "") or "").strip()
        model = item_model or global_model
        return provider_id, model, timeout

    async def _invoke(
        self,
        event: Any,
        section: str,
        item: dict,
        body: str,
        *,
        trigger: str = "",
        image_paths: Optional[list] = None,
        sources: Optional[list] = None,
        source: str = SOURCE_USER,
        notify_end: bool = True,
    ) -> TextToolResult:
        """调用一次对话模型并写入调用历史。

        「用户在群里发的扩写 / 提取」与「生图前的自动预处理」共用这一份实现：
        模板渲染、提供商/模型/超时解析、历史落库都只有这里一处。
        """
        spec = section_spec(section)
        image_paths = list(image_paths or [])
        sources = list(sources or [])
        trigger = trigger or str(item.get("trigger") or "")
        template = self._render_template(item, body, section)
        provider_id, model, timeout = self._resolve_llm(section, item)
        isolate = self._resolve_isolation(section, item)

        started = time.monotonic()
        call_meta: dict = {}
        result, reason = await self._command_llm(
            event,
            template=template,
            system_prompt=str(item.get("system_prompt", "") or ""),
            timeout=timeout,
            model=model,
            provider_id=provider_id,
            image_paths=image_paths,
            meta=call_meta,
            isolate=isolate,
        )
        duration_ms = (time.monotonic() - started) * 1000

        outcome = TextToolResult(
            text="",
            raw=str(result or ""),
            source=source,
            section=section,
            trigger=trigger,
            image_count=len(image_paths),
            image_source=",".join(dict.fromkeys(s for s in sources if s)),
            model=str(call_meta.get("model") or model or ""),
            duration_ms=duration_ms,
            template=template,
            body=body,
            system_prompt=str(item.get("system_prompt", "") or ""),
            reason=reason,
            meta={"provider_id": str(call_meta.get("provider_id") or provider_id or "")},
        )

        # 清理为提取形象落地的临时图片，避免留下一堆聊天图片
        remove_files(image_paths)

        recorder = (self.overrides or {}).get("_record_llm_call") or self.history_recorder
        if recorder is not None:
            try:
                recorder(event, outcome, item, result or "")
            except Exception:  # noqa: BLE001
                logger.debug("[neko_draw] 写入调用历史失败", exc_info=True)

        if not result:
            logger.warning(
                "[neko_draw] %s未返回内容（%s）：提供商=%s 模型=%s",
                spec["label"], reason or "未知原因",
                provider_id or "当前会话默认", model or "提供商默认",
            )
            outcome.text = f"{spec['label']}失败，请稍后重试"
            return outcome

        used_label = str(
            call_meta.get("model") or model or call_meta.get("provider_id") or provider_id or ""
        ).strip()
        # 自动预处理不追加“结束提示”：它不面向用户，只是一次内部调用
        end_hint = (
            scene_message(self.conf, "end_message", section, trigger, used_label)
            if notify_end else ""
        )
        outcome.text = f"{result}\n\n{end_hint}" if end_hint else result
        return outcome

    async def _notify_start(self, event: Any, section: str, trigger: str, model: str) -> None:
        """开始提示：让用户知道模型正在读图/扩写，避免以为没反应。

        发送失败只记日志，不影响后续调用（提示不是关键路径）。
        """
        hint = scene_message(self.conf, "start_message", section, trigger, model)
        if not hint:
            return
        try:
            await event.send(event.plain_result(hint))
        except Exception:  # noqa: BLE001
            logger.debug("[neko_draw] 开始提示发送失败", exc_info=True)
