# -*- coding: utf-8 -*-
"""发图统一出口。

三种发送方式（合并转发 / 图片外显金句 / 普通图片）在生图、APNG、后台补发
三处都要用，以前各写一遍且顺序不一致。这里统一成 :meth:`send_images`：

    开着合并转发 → 试合并转发
    失败或没开 → 试图片外显（仅 aiocqhttp 单图）
    再失败   → 普通图片（带可选的 @发送者）

APNG 成品与普通图片的唯一区别是读哪一组配置键（``apng_*`` 前缀）。
"""
from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.message_components import At, Image

from .config_store import get_bool
from .objects import cleanup_transient_apngs

PROFILE_DRAWING = "drawing"
PROFILE_APNG = "apng"


class ImageSender:
    """按配置策略把图片发到当前会话。"""

    def __init__(self, *, conf: Any, output: Any):
        self.conf = conf
        self.output = output

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def _key(profile: str, key: str) -> str:
        # 生图读默认键，APNG 读 apng_ 前缀键
        return f"apng_{key}" if profile == PROFILE_APNG else key

    def _flag(self, profile: str, key: str, default: bool = False) -> bool:
        # 用 get_bool 而不是 bool()：配置里出现字符串 "false"/"close" 时，
        # bool("false") 是 True，会让用户明明关了外显/合并转发却照样生效。
        return get_bool(self.conf, self._key(profile, key), default)

    def _quotes(self, profile: str) -> list[str]:
        """取该档位的图片外显金句。

        APNG 自己那组没填时才回落到生图那组，避免 APNG 没配文案时直接不发外显。
        """
        quotes = None
        if profile == PROFILE_APNG:
            quotes = list(getattr(self.output, "_apng_summary_quotes", None) or [])
        if not quotes:
            quotes = list(getattr(self.output, "_image_summary_quotes", None) or [])
        return quotes or ["[图片]"]

    def _random_quote(self, profile: str) -> str:
        quotes = self._quotes(profile)
        try:
            return random.choice(quotes)
        except (IndexError, TypeError):
            return "[图片]"

    # ------------------------------------------------------------------ 消息链
    def chain_with_at(self, event: Any, image_paths: list[str], profile: str = PROFILE_DRAWING) -> list:
        """构建（可选 @发送者 + 图片）的消息链。"""
        components = []
        in_group = False
        try:
            in_group = bool(event.get_group_id())
        except Exception:  # noqa: BLE001
            in_group = False
        if self._flag(profile, "enable_at_sender") and in_group:
            try:
                sender = str(event.get_sender_id() or "").strip()
                if sender:
                    components.append(At(qq=sender))
            except Exception:  # noqa: BLE001
                logger.debug("[neko_draw] 构造 @ 组件失败，跳过", exc_info=True)
        components.extend(Image.fromFileSystem(str(p)) for p in image_paths)
        return components

    # ------------------------------------------------------------------ 发送
    async def _send_forward(self, event: Any, paths: list[str], profile: str) -> bool:
        kwargs = {"profile": profile}
        if self._flag(profile, "enable_image_summary"):
            kwargs["summary"] = self._random_quote(profile)
        try:
            return bool(await self.output._send_forward_message(event, paths, **kwargs))
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 合并转发发送失败，回退普通图片", exc_info=True)
            return False

    async def _send_summary(self, event: Any, paths: list[str], profile: str) -> bool:
        try:
            return bool(
                await self.output._send_image_with_summary(event, paths, profile=profile)
            )
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 图片外显发送失败，回退普通图片", exc_info=True)
            return False

    async def send_images(
        self,
        event: Any,
        image_paths: list[str],
        *,
        profile: str = PROFILE_DRAWING,
        return_chain: bool = False,
    ) -> Any:
        """发送图片；``return_chain=True`` 时最后一步不直接发，而是把消息链交还调用方。

        生图主流程要 ``yield event.chain_result(chain)`` 才能保证回复顺序与框架
        回复策略一致；后台补发没有 yield 的机会，所以直接发。

        返回值：成功（含走了合并转发/图片外显）为 ``None``；``return_chain=True``
        时返回消息链；整条发送失败返回 ``False``。
        """
        if not image_paths:
            return None
        if self._flag(profile, "enable_forward_message"):
            if await self._send_forward(event, image_paths, profile):
                return None
        if self._flag(profile, "enable_image_summary"):
            if await self._send_summary(event, image_paths, profile):
                return None
        chain = self.chain_with_at(event, image_paths, profile)
        if return_chain:
            return chain
        try:
            await event.send(event.chain_result(chain))
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 图片发送失败", exc_info=True)
            # 明确告诉调用方“没发出去”，后台补发才能提示用户去历史里找回
            return False
        return None

    async def send_images_to_session(
        self, context: Any, umo: str, image_paths: list[str]
    ) -> bool:
        """把图片补发到指定会话（历史面板「找回」）。"""
        if not image_paths or not umo:
            return False
        try:
            from astrbot.api.event import MessageChain
        except ImportError:  # 极老的宿主版本
            logger.warning("[neko_draw] 当前 AstrBot 没有 MessageChain，无法补发到原会话")
            return False
        chain = MessageChain(chain=[Image.fromFileSystem(str(p)) for p in image_paths])
        try:
            return bool(await context.send_message(umo, chain))
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 补发到原会话失败：%s", umo, exc_info=True)
            return False

    async def wrap_apng(
        self, image_paths: list[str], apng_enabled, reference_images: Optional[list] = None
    ) -> list[str]:
        """按预设/全局开关做 APNG 包装，返回实际要发送的路径。

        ``reference_images`` 只在本地包装（``_wrap_apng`` 需要读参考图）时会用到，
        由调用方保证在包装期间仍然存在。
        """
        return await asyncio.to_thread(
            self.output._wrap_apng, image_paths, apng_enabled, reference_images
        )

    async def cleanup_transient(self, sent_paths: list[str], originals: list[str]) -> None:
        await asyncio.to_thread(cleanup_transient_apngs, sent_paths, originals)
