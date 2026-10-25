# -*- coding: utf-8 -*-
"""排队超时补偿：任务已提交但没等到结果时，转到后台继续等并补发。

这里的工作全部发生在 ``on_message`` 返回之后，不能再用 ``yield``，
所以所有发送都走 ``event.send`` / ``context.send_message``。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from astrbot.api import logger

from .config_store import get_int
from .image_sender import PROFILE_DRAWING, ImageSender
from .objects import (
    RequestContext,
    apng_enabled_from_choice,
    event_umo,
    remove_files,
    resolve_apng_choice,
)


class PendingDelivery:
    """管理“排队中”任务的后续投递。"""

    def __init__(
        self,
        *,
        conf: Any,
        history: Any,
        output: Any,
        downloader: Any,
        sender: ImageSender,
        providers_getter,
        context: Any,
        history_getter=None,
    ):
        self.conf = conf
        self.history = history
        self.output = output
        self.downloader = downloader
        self.sender = sender
        self.providers_getter = providers_getter
        self.context = context
        self.history_getter = history_getter
        # 本次投递用的覆盖项（兼容层替身）：由 flow 在启动后台任务前写入。
        # 不缓存成构造期快照，每次投递读最新值。
        self.overrides: dict = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ 生命周期
    def spawn(self, coro) -> "asyncio.Task":
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def shutdown(self) -> None:
        """插件卸载时取消全部后台任务。"""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()

    # ------------------------------------------------------------------ 配置
    def _history(self):
        """当前生效的历史库（插件入口替换 history_store 时跟着变）。"""
        getter = self.history_getter
        if callable(getter):
            try:
                current = getter()
                if current is not None:
                    return current
            except Exception:  # noqa: BLE001
                pass
        return self.history

    def should_retain_history(self) -> bool:
        """与正常生图一致的保留规则：发送后不清理，且历史上限大于 0。"""
        return (
            not bool(self.conf.get("drawing_cleanup_after_send", False))
            and get_int(self.conf, "drawing_history_limit", 200) > 0
        )

    # ------------------------------------------------------------------ 写库
    def save_pending_record(
        self, event: Any, text: str, result, ctx: Optional[RequestContext], gen_start: float
    ) -> Optional[int]:
        """排队超时时先写一条「排队中」记录，返回记录 ID。"""
        from .objects import RequestContext as _Ctx

        if not isinstance(ctx, _Ctx):
            ctx = _Ctx.from_dict(ctx or {})
        if not ctx.apng_choice:
            # 历史里的选项快照可能来自旧版本：补上 APNG 三态
            ctx.apng_choice = resolve_apng_choice(getattr(result, "apng", ""))
            ctx.apng_enabled = apng_enabled_from_choice(ctx.apng_choice)
        if not self.should_retain_history() or not getattr(result, "model_template", ""):
            return None
        try:
            record_id = self._history().add(
                user_id=event.get_sender_id(),
                group_id=event.get_group_id() or "",
                trigger_word=result.trigger,
                prompt=text,
                params=getattr(result, "params", {}),
                model_template=result.model_template,
                provider=result.provider,
                model=result.model,
                refer_image_count=result.refer_image_count,
                status="pending",
                error_message="",
                task_id=result.task_id,
                umo=event_umo(event),
                generation_time_ms=max(0.0, (time.time() - gen_start) * 1000),
                resolved_prompt=str(ctx.resolved_prompt or ""),
                options=ctx.history_options(pending_stage="submitted"),
                pending=True,
            )
            self._history().enforce_limit(get_int(self.conf, "drawing_history_limit", 200))
            return int(record_id)
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 排队记录写入失败", exc_info=True)
            return None

    def _save_finished(
        self,
        event: Any,
        text: str,
        result,
        image_paths: list[str],
        error: str,
        thumbnails: list[str],
        gen_ms: float,
        ctx: RequestContext,
        record_id: Optional[int] = None,
    ) -> bool:
        """回填后台补发的历史记录（成功失败都记），返回是否落库成功。"""
        if not self.should_retain_history() or not result.model_template:
            return False
        status = "success" if image_paths else "failed"
        options = ctx.history_options(pending_stage="done")
        try:
            if record_id:
                ok = self._history().finish_pending(
                    int(record_id),
                    image_paths=image_paths,
                    image_thumbnail_paths=list(thumbnails or []),
                    status=status,
                    error_message="" if image_paths else error,
                    task_id=result.task_id,
                    generation_time_ms=gen_ms,
                )
                if ok:
                    self._history().update_extra(int(record_id), options=options)
                    return True
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
                status=status,
                error_message="" if image_paths else error,
                image_paths=image_paths,
                image_thumbnail_paths=list(thumbnails or []),
                task_id=result.task_id,
                umo=event_umo(event),
                generation_time_ms=gen_ms,
                resolved_prompt=str(ctx.resolved_prompt or ""),
                options=options,
            )
            self._history().enforce_limit(get_int(self.conf, "drawing_history_limit", 200))
            return True
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 后台补发历史写入失败", exc_info=True)
            return False

    # ------------------------------------------------------------------ 投递
    async def deliver(
        self,
        event: Any,
        text: str,
        result,
        gen_start: float,
        ctx: RequestContext,
        record_id: Optional[int] = None,
    ) -> None:
        """后台等待超时任务的结果：成功补发原图并回填历史，失败则说明原因。"""
        pending = result.pending
        if pending is None:
            return
        local_paths: list[str] = []
        detail = ""
        references = list(ctx.source_images or [])
        try:
            urls = await pending.client.resume(pending.task_id, pending.key)
            for url in urls:
                path = await self.downloader.download(
                    url, proxy=getattr(pending.client, "proxy", None)
                )
                if path:
                    local_paths.append(path)
            if not urls:
                detail = "生成完成，但未返回图片"
            elif not local_paths:
                detail = "生成完成，但图片下载失败"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            detail = str(e) or "任务最终未能完成"
            logger.warning("[IMAGE] 后台等待任务失败：任务ID=%s：%s", pending.task_id, e)
            if "超时" in detail or "timeout" in detail.lower():
                detail = "排队太久啦，请稍后再试"

        gen_ms = max(0.0, (time.time() - gen_start) * 1000)
        if detail:
            await self._notify(event, f"图片生成失败：{detail}")
            self._save_finished(event, text, result, [], detail, [], gen_ms, ctx, record_id=record_id)
            # 失败时参考图同样用不到了，一并清掉（否则重试一次就多留一批临时图）
            if references:
                await asyncio.to_thread(remove_files, references)
            return

        thumbnails = await asyncio.to_thread(
            self.output.save_history_thumbnails, local_paths, "output"
        )
        saved = self._save_finished(
            event, text, result, local_paths, "", thumbnails, gen_ms, ctx, record_id=record_id
        )
        # 补发时沿用预设的 APNG 选择；参考图沿用用户当初发的那几张临时副本。
        # 包装失败（例如本地首帧图损坏）也要照常清理临时文件并告诉用户，
        # 不能让异常从后台任务里直接冒出来、图既不补发也不解释。
        sent: list[str] = list(local_paths)
        fake_send = (self.overrides or {}).get("_send_pending_images")
        delivered = False
        try:
            try:
                sent = await self.sender.wrap_apng(local_paths, ctx.apng_enabled, references)
            except Exception:  # noqa: BLE001
                logger.warning("[neko_draw] 补发时 APNG 包装失败，改发原图", exc_info=True)
                sent = list(local_paths)
            if fake_send:
                outcome = fake_send(event, sent, ctx.apng_enabled, references)
                if hasattr(outcome, "__await__"):
                    await outcome
                delivered = True
            else:
                # send_images 成功、走了合并转发/外显时返回 None；整条发送失败返回 False
                delivered = await self.sender.send_images(event, sent, profile=PROFILE_DRAWING) is not False
        finally:
            await self.sender.cleanup_transient(sent, local_paths)
            # 参考图的临时副本只有补发会用到，结束就删掉，避免在 save_images 里越积越多
            if references:
                await asyncio.to_thread(remove_files, references)
        if not saved:
            await asyncio.to_thread(self.output.cleanup_generated_images, local_paths)
        # 以前这两种失败都是静默的：图没发出去或历史没记上，用户毫不知情
        if not delivered:
            await self._notify(
                event, "图片已经生成好了，但补发到本会话失败；可在 WebUI 历史里点「找回」重试"
            )
        elif not saved:
            await self._notify(
                event, "图片已发出，但这次没能记进历史（可检查「历史上限」与磁盘剩余空间）"
            )

    # ------------------------------------------------------------------ 杂项
    def _send(self, event, message):
        """兼容层给的发送替身优先（旧断言依赖它），否则直接用 event.send。"""
        fake = (self.overrides or {}).get("_notify_event")
        if callable(fake):
            return fake(event, message)
        return event.send(event.plain_result(message))

    async def _notify(self, event: Any, message: str) -> None:
        if not message:
            return
        try:
            outcome = self._send(event, message)
            if hasattr(outcome, "__await__"):
                await outcome
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 后台补发提示发送失败", exc_info=True)

    # ------------------------------------------------------------------ 找回
    async def fetch_task_result(self, task_id: str) -> list[str]:
        """在全部 RunningHub 提供商的全部 Key 上尝试查询任务结果。

        平台按 Key 隔离任务：必须用当初提交该任务的 Key 才能查到，因此逐个试。
        """
        for name, client in (self.providers_getter() or {}).items():
            fetch = getattr(client, "fetch_result", None)
            if not callable(fetch):
                continue
            keys = list(getattr(client, "api_keys", [None]) or [None])
            for key in keys:
                try:
                    urls = await fetch(task_id, key)
                except Exception as e:  # noqa: BLE001
                    logger.debug("[neko_draw] 找回查询未命中 %s: %s", name, e)
                    continue
                if urls:
                    return urls
        return []

    async def recall(self, record_id: int, deliver: bool = True) -> dict:
        """按历史记录里的任务 ID 到 RunningHub 取回生成图，并（可选）补发到原会话。"""
        record = await asyncio.to_thread(self._history().get, int(record_id))
        if not record:
            raise ValueError("记录不存在")
        task_id = str(record.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("该记录没有任务 ID，无法找回（旧版本记录，或不是 RunningHub 任务）")

        urls = await self.fetch_task_result(task_id)
        if not urls:
            raise ValueError(
                "平台上查不到该任务的成品（可能仍在排队、已被平台清理，"
                "或不是本账号的 Key 提交的）"
            )
        local_paths: list[str] = []
        for url in urls:
            path = await self.downloader.download(url)
            if path:
                local_paths.append(path)
        if not local_paths:
            raise ValueError("已找回结果，但图片下载失败，请稍后重试")

        thumbnails = await asyncio.to_thread(
            self.output.save_history_thumbnails, local_paths, "output"
        )
        # 用 finish_pending 而不是 update_images：找回的记录如果原先处于“排队中”，
        # update_images 不会清 pending 标记，那条记录会永远留在「排队中」筛选里。
        await asyncio.to_thread(
            self._history().finish_pending,
            int(record_id),
            image_paths=local_paths,
            image_thumbnail_paths=thumbnails,
            status="success",
            error_message="",
        )

        umo = str(record.get("umo") or "").strip()
        options = record.get("options") if isinstance(record.get("options"), dict) else {}
        apng_enabled = options.get("apng_enabled") if "apng_enabled" in options else None
        delivered = False
        if deliver and umo:
            sent = local_paths
            try:
                # 包装失败不能把“找回”整条打断：历史已经标成功，图也下载好了，
                # 退回原图发送，并保证临时 APNG 文件被清理。
                try:
                    sent = await self.sender.wrap_apng(local_paths, apng_enabled, None)
                except Exception:  # noqa: BLE001
                    logger.warning("[neko_draw] 找回时 APNG 包装失败，改发原图", exc_info=True)
                    sent = local_paths
                delivered = await self.sender.send_images_to_session(self.context, umo, sent)
            finally:
                await self.sender.cleanup_transient(sent, local_paths)
        logger.info(
            "[IMAGE] 找回完成：记录#%s 任务ID=%s 图片=%d 补发=%s",
            record_id, task_id, len(local_paths), delivered,
        )
        return {
            "count": len(local_paths),
            "images": local_paths,
            "delivered": delivered,
            "deliver_available": bool(umo),
        }
