# -*- coding: utf-8 -*-
"""绘图消息主流程：从“收到一条消息”到“发出图片或提示”。

这里只做编排，具体动作都委托给专门的模块：

- 取图 → ``ImageCollector``
- 生图前的文字预处理 → ``TextToolService``（复用「扩写」指令）
- 扩写/提取 → ``TextToolService``
- 调模型出图 → ``core.handler.DrawingHandler``
- 发图 → ``ImageSender``
- 排队补发 → ``PendingDelivery``
- APNG → ``ApngService``

分流顺序与旧版保持一致（APNG → 内部指令 → 白名单 → 扩写/提取 → 生图），
只额外修了两处在真实使用中暴露的问题：

1. 触发词前面带 @（``@铃 提取`` / ``@某人 提取``）以前会静默失败；
2. 合并转发里的图在正文为空时会走到“提示词不能为空”。

`/nc` 指令的具体实现在 ``core.commands`` 里，这里只负责消息分流，不重复维护子指令清单。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional

from astrbot.api import logger
from astrbot.api.message_components import At, Plain

from .apng import ApngService
from .config_store import get_bool, get_int
from .handler import STAGE_START
from .image_collector import ImageCollector
from .image_sender import PROFILE_DRAWING, ImageSender
from .objects import (
    DECOMPOSE_DISABLED,
    DECOMPOSE_FALLBACK,
    DECOMPOSE_SUCCESS,
    build_request_context,
    event_umo,
    remove_files,
    resolve_apng_choice,
)
from .parser import ParsedPrompt, parse_prompt_message
from .pending_delivery import PendingDelivery
from .text_tools import TextToolService
from .text_utils import split_command, strip_leading_at

# 本插件自己的斜杠命令：交给框架的 command 处理器，这里必须直接放行
INTERNAL_COMMANDS = ("apng", "nc")


@dataclass
class FlowDeps:
    """主流程依赖的服务集合（由插件入口组装一次）。"""

    conf: Any
    prompt_manager: Any
    whitelist: Any
    rate_limiter: Any
    generation: Any
    """生图实现（core.handler.DrawingHandler）"""
    collector: ImageCollector
    sender: ImageSender
    apng: ApngService
    text_tools: TextToolService
    pending: PendingDelivery
    output: Any
    history: Any
    llm_history: Any
    expand_store_getter: Any
    overrides: dict
    runtime: Any = None
    history_getter: Any = None


class MessageFlow:
    """一条消息的完整处理流程。"""

    def __init__(self, deps: FlowDeps):
        self.deps = deps

    # ------------------------------------------------------------------ 小工具
    @property
    def conf(self):
        return self.deps.conf

    @property
    def prompt_manager(self):
        """当前生效的提示词管理器。

        优先取 runtime 上的实例：``plugin.prompt_manager = ...`` 会写回 runtime，
        这样替换后生图与扩写/提取都能立刻看到新预设。
        """
        runtime = getattr(self.deps, "runtime", None)
        current = getattr(runtime, "prompt_manager", None) if runtime is not None else None
        return current or self.deps.prompt_manager

    def _sync_prompt_manager(self) -> None:
        """把 flow 当前生效的 prompt_manager 同步给文本管线。

        生图与扩写/提取必须看到同一份预设，否则会出现“生图认得触发词、
        扩写却说不认识”的错位。
        """
        try:
            self.deps.text_tools.prompt_manager = self.prompt_manager
        except Exception:  # noqa: BLE001
            pass

    def _whitelisted(self, event: Any) -> bool:
        try:
            return bool(
                self.deps.whitelist.check(event.get_sender_id(), event.get_group_id())
            )
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 白名单检查失败，按不放行处理", exc_info=True)
            return False

    def _should_at_sender(self) -> bool:
        return get_bool(self.conf, "command_end_at_sender", True)

    @staticmethod
    def _at_chain_result(event: Any, sender: str, reply: str):
        """在回复前带上 @发送者。

        At 的 qq 与 Plain 的 text 必须是字符串：Plain 是 pydantic 模型，
        收到非字符串会直接抛 ValidationError，整条回复就发不出去了。
        """
        text = str(reply if reply is not None else "")
        qq = str(sender if sender is not None else "").strip()
        if qq:
            try:
                return event.chain_result([At(qq=qq), Plain(f" {text}")])
            except Exception:  # noqa: BLE001
                logger.debug("[neko_draw] @ 发送者失败，退回纯文字", exc_info=True)
        return event.plain_result(text)

    def _text_reply(self, event: Any, reply: str):
        """文字类回复（扩写/提取/限流提示）：按开关决定是否 @ 发送者。

        与图片的 ``enable_at_sender`` 是两套开关：文字走 `command_end_at_sender`，
        图片走 `enable_at_sender`，互不影响。
        """
        sender = str(event.get_sender_id() or "").strip()
        if self._should_at_sender() and sender:
            return self._at_chain_result(event, sender, reply)
        return event.plain_result(str(reply))

    # ------------------------------------------------------------------ 入口
    async def handle(self, event: Any) -> AsyncGenerator:
        """AstrBot 消息入口（generator）。"""
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text:
            return

        # 1) APNG：兼容带/不带斜杠
        if split_command(text, ["apng"]) is not None:
            if self._whitelisted(event):
                fake_run = (self.deps.overrides or {}).get("_handle_apng")
                if fake_run:
                    # 兼容替换：旧接口返回“可直接 yield 的回复对象”
                    response = await fake_run(event)
                    if response is not None:
                        yield response
                    return
                message = await self.deps.apng.run(event)
                if message is not None:
                    # 参数错 / 张数不够这类提示：与扩写、限流一样走 _text_reply，
                    # 让“是否 @ 发送者”只有一处规则
                    yield self._text_reply(event, message)
            return

        # 2) 本插件自己的斜杠命令交给框架，避免重复处理
        if self._is_internal_command(text):
            return

        # 3) 允许 /触发词 写法
        if text.startswith("/"):
            text = text[1:].strip()
            if not text:
                return

        # 4) 触发词前面被 @ 挡住（@铃 提取 / @某人 提取）：剥离开头的提及再匹配
        text = strip_leading_at(text)
        if not text:
            return

        # 5) 扩写 / 提取形象：命中即只回文字
        self._sync_prompt_manager()
        for section in ("extract", "expand"):
            item = self.deps.text_tools.match(section, text)
            if item is None:
                continue  # 没命中触发词：继续看下一个分区
            # 停用的条目仍会被 match 返回（列表/详情要展示它），但不响应消息。
            # 用 continue 而不是 return：让用户能接着用同一条消息触发生图。
            if item.get("enabled", True) is False:
                continue
            if not self._whitelisted(event):
                return
            outcome = await self.deps.text_tools.run(event, section, text, item=item)
            yield self._text_reply(event, outcome.text)
            return

        # 6) 生图（白名单在 handler 内部校验，这里不再重复）
        parsed = parse_prompt_message(text, self.prompt_manager.get_all())
        if parsed is None:
            return
        async for item in self._drawing(event, text, parsed):
            yield item

    @staticmethod
    def _is_internal_command(text: str) -> bool:
        value = str(text or "").strip()
        return any(
            value == f"/{cmd}" or value.startswith(f"/{cmd} ")
            for cmd in INTERNAL_COMMANDS
        )

    # ------------------------------------------------------------------ 生图
    async def _drawing(self, event: Any, text: str, parsed: Optional[ParsedPrompt] = None) -> AsyncGenerator:
        # 拍一份快照：一次请求中途被外部改动替换项，不应影响本次处理
        overrides = dict(self.deps.overrides or {})
        if parsed is None:
            parsed = parse_prompt_message(text, self.prompt_manager.get_all())
        if parsed is None:
            return
        preset = self.prompt_manager.get_by_trigger(parsed.trigger)

        # 取图：按采图优先级，命中一个来源后取全部
        collected = await self.deps.collector.collect(
            event, overrides=ImageCollector.overrides_from(overrides)
        )
        image_urls = list(collected.paths)
        source_temporary_paths = list(collected.temporary)

        # 预设风格参考图：匹配到预设时自动加入参考图
        preset_ref_result = self.prompt_manager.get_reference_image_for_text(text)
        if preset_ref_result is not None:
            preset_ref_raw, position = preset_ref_result
            preset_ref_path = self.deps.output._resolve_file_ref(preset_ref_raw)
            if preset_ref_path is not None:
                ref = str(preset_ref_path)
                if position == "after":
                    image_urls = image_urls + [ref]
                else:
                    image_urls = [ref] + image_urls

        # 正文为空：图片编辑模型允许“只发图 + 触发词”（图片本身就是指令），
        # 这个判断在 handler 里选出模板之后进行；这里不再伪造 parsed（以前改了局部
        # 变量却没传给 handler，等于没修）。

        async def _progress(stage: str) -> None:
            if stage != STAGE_START:
                return
            if not get_bool(self.conf, "enable_drawing_message", True):
                return
            message = str(
                self.conf.get("drawing_message", "小猫正在搓屏幕中/ᐠ - ˕ -マ Ⳋ📱")
            ).strip()
            if message:
                await event.send(event.plain_result(message))

        # 限流（通过则记账；失败在 finally 释放）
        reservation, triggered_rule, retry_after = self.deps.rate_limiter.reserve(
            event.get_sender_id()
        )
        if triggered_rule:
            window_desc = self.deps.rate_limiter.format_window(triggered_rule["window_seconds"])
            max_count = triggered_rule["max_count"]
            template = str(self.conf.get(
                "rate_limit_message",
                "🐱 绘图频率过高，{window}内最多{max_count}次，请{retry_after}秒后再试",
            ))
            try:
                message = template.format(
                    window=window_desc, max_count=max_count, retry_after=retry_after
                )
            except (KeyError, IndexError, ValueError):
                message = f"🐱 绘图频率过高，{window_desc}内最多{max_count}次，请{retry_after}秒后再试"
            remove_files(source_temporary_paths)
            # 与其它文字提示走同一条规则：是否 @ 发送者由 command_end_at_sender 决定，
            # 以前这里无条件 @，配置关掉也照样带 @。
            yield self._text_reply(event, message)
            return

        # 生图前的文字预处理（只在真正要出图前调用，避免空耗额度）
        fake_should = self._override("_should_decompose")
        should = fake_should or self.deps.text_tools.should_preprocess
        decompose_requested = bool(should(preset))

        tags = ""
        decompose_error = ""
        tag_override: Optional[str] = None
        if decompose_requested:
            # 预处理开启时 handler 会用 user_text_override 重新解析；这里保持同一个触发词，
            # 只刷新预设引用（预设可能在插件运行期间被替换过）。
            preset = self.prompt_manager.get_by_trigger(parsed.trigger)
            fake_decompose = overrides.get("_decompose_tags")
            decompose = fake_decompose or self.deps.text_tools.preprocess
            tags, decompose_error = await decompose(parsed.user_body, event, preset)
            if tags:
                tag_override = tags
            elif not get_bool(self.conf, "llm_tag_fallback", True):
                # 关闭“失败回退”时：不出图、不占额度，并说明原因。
                # 回执走 _text_reply，与其它文字回复同一条规则（是否 @ 发送者）。
                self._finish_quota(reservation, False)
                remove_files(source_temporary_paths)
                reason = decompose_error or "未知原因"
                yield self._text_reply(
                    event,
                    f"❌ 提示词预处理失败（{reason}），请稍后重试，"
                    "或在「文字加工 → 文字加工配置」里打开「预处理失败时回退原文本」",
                )
                return
            else:
                tag_override = parsed.user_body
                logger.info(
                    "[neko_draw] 预处理未得到可用结果（%s），回退为原文提示词",
                    decompose_error or "未知原因",
                )
                if decompose_error:
                    await self._notify(
                        event,
                        f"⚠️ 这次的提示词没能自动预处理（{decompose_error}），"
                        "已改用你输入的原文直接出图",
                    )

        if tags:
            decompose_status = DECOMPOSE_SUCCESS
        elif decompose_requested:
            decompose_status = DECOMPOSE_FALLBACK
        else:
            decompose_status = DECOMPOSE_DISABLED

        logger.info(
            "[neko_draw] 预处理状态：请求=%s 错误=%s 是否改成原文=%s",
            "是" if decompose_requested else "否",
            decompose_error or "-",
            "是" if tag_override else "否",
        )

        gen_start = time.time()
        result = None  # core/handler.py 的 HandlerResult
        source_history_paths: list[str] = []
        source_thumbnail_paths: list[str] = []
        output_thumbnail_paths: list[str] = []
        retain_history = self._should_retain_history()
        try:
            result = await self.generation.handle(
                text,
                image_urls,
                user_id=event.get_sender_id(),
                group_id=event.get_group_id(),
                progress_cb=_progress,
                user_text_override=tag_override,
            )
            if retain_history and result and result.refer_image_count and image_urls:
                source_history_paths = await asyncio.to_thread(
                    self.deps.output.save_history_sources, image_urls
                )
                source_thumbnail_paths = await asyncio.to_thread(
                    self.deps.output.save_history_thumbnails, source_history_paths, "input"
                )
        finally:
            self._finish_quota(reservation, bool(result is not None and result.images))
            # 排队时后台补发还要渲染参考图，所以这几张临时图交给它清理；
            # 其它情况（成功 / 失败 / 只看拆解）在这里立刻删掉。
            queued = result is not None and getattr(result, "pending", None) is not None
            if source_temporary_paths and not queued:
                remove_files(source_temporary_paths)

        if result is None:
            return
        if result.silent:
            return
        gen_ms = (time.time() - gen_start) * 1000

        ctx = build_request_context(
            result,
            trigger=result.trigger or parsed.trigger,
            apng_choice=resolve_apng_choice(
                getattr(result, "apng", ""), (preset or {}).get("apng", "")
            ),
            decompose_requested=decompose_requested,
            decompose_status=decompose_status,
            decompose_error=decompose_error,
            tags=tags if decompose_status == DECOMPOSE_SUCCESS else "",
            umo=event_umo(event),
        )

        # 任务已提交但排队超时：转后台继续等，出图后自动补发
        if result.pending is not None:
            # 覆盖项要一起带进线程：to_thread 里读不到事件循环线程的临时状态
            self.deps.pending.overrides = overrides
            # 排队补发时才需要参考图的临时副本（本地包装 APNG 时会用到）
            ctx.source_images = list(source_temporary_paths)
            record_id = await asyncio.to_thread(
                self.deps.pending.save_pending_record, event, text, result, ctx, gen_start
            )
            if source_history_paths or source_thumbnail_paths:
                await asyncio.to_thread(
                    self.deps.output.cleanup_history_assets,
                    source_history_paths + source_thumbnail_paths,
                )
            notice = str(self.conf.get(
                "pending_message", "小猫还在排队等出图，出来马上补发原图/ᐠ - ˕ -マ Ⳋ"
            ) or "").strip()
            if notice:
                yield event.plain_result(notice)
            spawn = overrides.get("_spawn_pending_delivery")
            if spawn is None:
                self.deps.pending.spawn(
                    self.deps.pending.deliver(
                        event, text, result, gen_start, ctx, record_id=record_id
                    )
                )
            else:
                # 兼容旧签名：(event, text, result, gen_start, **kwargs)
                spawn(event, text, result, gen_start, request=ctx.as_dict(), record_id=record_id)
            return

        # 写历史（成功失败都记，静默忽略的不记）
        history_saved = False
        if retain_history and result.model_template:
            try:
                if result.images:
                    output_thumbnail_paths = await asyncio.to_thread(
                        self.deps.output.save_history_thumbnails, result.images, "output"
                    )
                self._history().add(
                    user_id=event.get_sender_id(),
                    group_id=event.get_group_id() or "",
                    trigger_word=result.trigger,
                    prompt=text,
                    params=result.params,
                    model_template=result.model_template,
                    provider=result.provider,
                    model=result.model,
                    refer_image_count=result.refer_image_count,
                    status="success" if result.images else "failed",
                    error_message="" if result.images else result.text,
                    image_paths=result.images,
                    source_image_paths=source_history_paths,
                    image_thumbnail_paths=output_thumbnail_paths,
                    source_thumbnail_paths=source_thumbnail_paths,
                    task_id=result.task_id,
                    umo=event_umo(event),
                    generation_time_ms=gen_ms,
                    resolved_prompt=str(ctx.resolved_prompt or ""),
                    options=ctx.history_options(),
                )
                self._history().enforce_limit(get_int(self.conf, "drawing_history_limit", 200))
                history_saved = True
            except Exception:  # noqa: BLE001
                logger.warning("[neko_draw] 历史记录写入失败", exc_info=True)
                self.deps.output.cleanup_history_assets(
                    source_history_paths + source_thumbnail_paths + output_thumbnail_paths
                )

        if result.images:
            # 包装失败不能连图一起丢掉：退回原图继续发，用户至少能拿到成品
            sent_paths: list[str] = list(result.images)
            try:
                sent_paths = await self.deps.sender.wrap_apng(
                    result.images, ctx.apng_enabled, None
                )
            except Exception:  # noqa: BLE001
                logger.warning("[neko_draw] APNG 包装失败，改发原图", exc_info=True)
                sent_paths = list(result.images)
            fake_send = overrides.get("_send_pending_images")
            try:
                if fake_send:
                    outcome = fake_send(event, sent_paths, ctx.apng_enabled, None)
                    if hasattr(outcome, "__await__"):
                        await outcome
                    chain = None
                else:
                    chain = await self.deps.sender.send_images(
                        event, sent_paths, profile=PROFILE_DRAWING, return_chain=True
                    )
                if chain is not None:
                    yield event.chain_result(chain)
            finally:
                await self.deps.sender.cleanup_transient(sent_paths, result.images)
                if not history_saved:
                    await asyncio.to_thread(
                        self.deps.output.cleanup_generated_images, result.images
                    )
        elif result.text:
            yield event.plain_result(result.text)

    # ------------------------------------------------------------------ 覆盖
    def _override(self, name: str):
        return (self.deps.overrides or {}).get(name)

    @property
    def generation(self):
        """当前生效的生图实现。

        优先取 runtime.handler：插件实例上 ``plugin.handler = ...`` 会写回 runtime，
        这样替换后主流程立刻用新实现，不会继续用初始化时捕获的旧对象。
        """
        runtime = getattr(self.deps, "runtime", None)
        current = getattr(runtime, "handler", None) if runtime is not None else None
        return current or self.deps.generation

    def _history(self):
        """当前生效的历史库（插件实例上的 history_store 可能被替换）。"""
        getter = self.deps.history_getter
        if callable(getter):
            try:
                current = getter()
                if current is not None:
                    return current
            except Exception:  # noqa: BLE001
                pass
        return self.deps.history

    # ------------------------------------------------------------------ 辅助
    def _finish_quota(self, reservation, success: bool) -> None:
        """结账或释放限流名额；没启用限流时 reservation 为 None，直接跳过。"""
        if reservation is None:
            return
        try:
            self.deps.rate_limiter.finish(reservation, success)
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 限流结账失败", exc_info=True)

    def _should_retain_history(self) -> bool:
        return (
            not get_bool(self.conf, "drawing_cleanup_after_send", False)
            and get_int(self.conf, "drawing_history_limit", 200) > 0
        )

    async def _notify(self, event: Any, message: str) -> None:
        fake = (self.deps.overrides or {}).get("_notify_event")
        if fake is not None:
            await fake(event, message)
            return
        if not message:
            return
        try:
            await event.send(event.plain_result(message))
        except Exception:  # noqa: BLE001
            logger.debug("[neko_draw] 提示发送失败", exc_info=True)
