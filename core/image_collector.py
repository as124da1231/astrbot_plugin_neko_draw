# -*- coding: utf-8 -*-
"""统一图片采集：消息图 / 引用图 / 合并转发图 / @头像 → 本地文件。

以前这套逻辑分散在 ``main.py`` 的 4 个方法里（_extract_image_urls、
_extract_quoted_image_urls、_extract_forward_image_urls、_extract_at_avatar_url），
合并转发时经常出现“取到一半就停”的问题。这里集中成一个类，规则只有一条：

    先按「采图优先级」挑出**一个**来源，再把这个来源里的图**全部**取回。

这样既不会出现“本条消息只有图 A，就把引用消息里的图 B 丢掉”，
也不会出现同一张图被多个解析器重复收集。
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.message_components import At, File, Image, Reply

from .config_store import get_bool
from .forward_images import extract_forward_ids, extract_payload_images
from .image_dedupe import image_content_digest
from .image_extract import extract_image_urls

# 来源顺序（也是 WebUI 浮块的默认顺序）
SOURCE_MESSAGE = "message"
SOURCE_QUOTED = "quoted"
SOURCE_FORWARD = "forward"
SOURCE_AVATAR = "avatar"
IMAGE_SOURCE_ORDER = [SOURCE_MESSAGE, SOURCE_QUOTED, SOURCE_FORWARD, SOURCE_AVATAR]

AVATAR_URL_TEMPLATE = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640"

# 合并转发展开的最大嵌套层数（防止恶意的自引用转发）
MAX_FORWARD_DEPTH = 3


@dataclass
class CollectedImages:
    """一次采图的结果。"""

    paths: list[str] = field(default_factory=list)
    source: str = ""                 # 命中的来源（message/quoted/forward/avatar），空表示没取到
    temporary: list[str] = field(default_factory=list)   # 落在 save_dir 的临时文件，用完要删

    def __bool__(self) -> bool:
        return bool(self.paths)


def parse_priority(raw: Any) -> list[str]:
    """把「采图优先级」配置解析成有序来源列表。

    配置里用逗号分隔，可写 ``message,quoted,forward,avatar``；中文逗号也认。
    去重后按书写顺序返回，未知单词直接忽略；全空时回落默认顺序。
    """
    value = str(raw or "").replace("，", ",")
    tokens: list[str] = []
    for token in (t.strip().lower() for t in value.split(",")):
        if token in IMAGE_SOURCE_ORDER and token not in tokens:
            tokens.append(token)
    return tokens or list(IMAGE_SOURCE_ORDER)


class ImageCollector:
    """按优先级从一条消息里取图，并统一落地成插件自己的临时文件。"""

    def __init__(self, *, downloader: Any, save_dir: Path, conf: Any):
        self.downloader = downloader
        self.save_dir = Path(save_dir)
        self.conf = conf

    # ------------------------------------------------------------------ 配置
    def priority(self, override: Any = None) -> list[str]:
        """当前生效的采图优先级。

        ``override`` 可以是字符串（覆盖配置），也可以是一份覆盖表（兼容层替身），
        两种都给时字符串优先——调用点可以无脑把整份 overrides 传进来。
        """
        if isinstance(override, dict):
            override = override.get("extract_image_priority")
        raw = override if isinstance(override, str) and override.strip() else self.conf.get(
            "extract_image_priority", ""
        )
        return parse_priority(raw)

    def avatar_allowed(self, override: Any = None) -> bool:
        """@ 头像能否作为图源：总开关 + 采图优先级里保留 avatar。

        总开关按配置读取规则判（``"false"`` / ``"关"`` 都算关闭），
        避免字符串 ``"false"`` 被当成真值，出现“关了头像还在取”。
        """
        if get_bool(self.conf, "extract_at_avatar", True) is False:
            return False
        return SOURCE_AVATAR in self.priority(override)

    # ------------------------------------------------------------------ 提示
    def missing_hint(self, override: Any = None) -> str:
        """取不到图时，按当前实际开启的来源逐条给出可操作建议。

        只提真的能用的方式：采图优先级里没保留 `avatar`（或总开关关掉）时，
        不会让用户去 @ 一个人。``override`` 可以是一份 overrides 覆盖表。
        """
        order = self.priority(override)
        hints: list[str] = []
        if SOURCE_MESSAGE in order:
            hints.append("直接发送图片")
        if SOURCE_QUOTED in order:
            hints.append("回复一张图片")
        if SOURCE_FORWARD in order:
            hints.append("发一条合并转发的消息")
        if self.avatar_allowed(override):
            hints.append("@ 一个人用 TA 的头像")
        if not hints:
            return "当前采图优先级没有任何可用来源，请先在 WebUI 的「采图优先级」里开启至少一项"
        if len(hints) == 1:
            return f"当前只开启了「{hints[0]}」这一种取图方式，请按这种方式发送图片"
        return "，".join(hints[:-1]) + " 或 " + hints[-1]

    # ------------------------------------------------------------------ 单来源
    @classmethod
    def message_refs(cls, event: Any) -> list[str]:
        """本条消息里的图片引用（含 Reply.chain 与 File）。"""
        messages = cls._messages(event)
        return extract_image_urls(
            messages,
            image_types=(Image,),
            reply_types=(Reply,),
            file_types=(File,),
        )

    async def quoted_refs(self, event: Any) -> list[str]:
        """引用消息里的图片。

        AstrBot 4.28+ 自带的引用解析器能通过 reply id 重新取原消息，
        能处理 Reply.chain 为空、base64:// 和 QQ file ID 等平台差异。
        """
        try:
            from astrbot.core.utils.quoted_message import extract_quoted_message_images
        except (ImportError, AttributeError):
            return self._quoted_chain_refs(event)
        except Exception:  # noqa: BLE001
            return self._quoted_chain_refs(event)
        try:
            urls = await extract_quoted_message_images(event)
        except Exception:  # noqa: BLE001
            logger.debug("[IMAGE] AstrBot 引用图片解析器未返回结果", exc_info=True)
            urls = None
        refs = [str(u) for u in (urls or []) if str(u).strip()]
        if refs:
            return refs
        # 解析器没拿到内容时，退回 Reply.chain（很多适配器把图放在这里）
        return self._quoted_chain_refs(event)

    @staticmethod
    def _quoted_chain_refs(event: Any) -> list[str]:
        messages = ImageCollector._messages(event)
        refs: list[str] = []
        for component in messages:
            if not isinstance(component, Reply):
                continue
            sub = getattr(component, "chain", None)
            if sub:
                refs.extend(extract_image_urls(sub, image_types=(Image,)))
        return refs

    @staticmethod
    def _messages(event: Any) -> list:
        """安全地取消息链：事件对象缺字段时返回空列表而不是抛异常。"""
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception:  # noqa: BLE001
            return []

    @classmethod
    def raw_message(cls, event: Any) -> Any:
        message_obj = getattr(event, "message_obj", None)
        return getattr(message_obj, "raw_message", None)

    @staticmethod
    def _bot_caller(event: Any):
        bot = getattr(event, "bot", None)
        caller = getattr(getattr(bot, "api", None), "call_action", None)
        if callable(caller):
            return caller
        caller = getattr(bot, "call_action", None)
        return caller if callable(caller) else None

    async def forward_refs(self, event: Any) -> list[str]:
        """展开 QQ/OneBot 合并转发，按节点顺序收集全部图片。

        引用里套合并转发（``引用消息 → 转发 → 图片``）也能取到：先收本条消息里
        的转发 ID，再收引用消息链里的转发 ID。
        """
        queue: list[Any] = []
        sources = [self._messages(event), self.raw_message(event)]
        for source in sources:
            queue.extend(self._safe_forward_ids(source))
        # 被引用的那条消息本身可能就是一张合并转发卡片
        for source in self.quoted_chains(event):
            queue.extend(self._safe_forward_ids(source))
        if not queue:
            return []
        caller = self._bot_caller(event)
        if caller is None:
            return []

        images: list[str] = []
        visited: set[str] = set()
        current = self._flatten(queue, 0)
        while current:
            forward_id, depth = current.pop(0)
            if not forward_id or forward_id in visited or depth > MAX_FORWARD_DEPTH:
                continue
            visited.add(forward_id)
            payload = await self._fetch_forward(caller, forward_id)
            if not payload:
                continue
            images.extend(extract_payload_images(payload))
            # 嵌套转发最多展开 MAX_FORWARD_DEPTH 层，防止自引用/循环
            current.extend((value, depth + 1) for value in self._safe_forward_ids(payload))
        return images

    @staticmethod
    def _safe_forward_ids(value: Any) -> list[str]:
        try:
            return extract_forward_ids(value)
        except Exception:  # noqa: BLE001
            logger.debug("[IMAGE] 解析合并转发 ID 失败", exc_info=True)
            return []

    @staticmethod
    def quoted_chains(event: Any) -> list[Any]:
        messages = ImageCollector._messages(event)
        chains = []
        for component in messages:
            if isinstance(component, Reply):
                sub = getattr(component, "chain", None)
                if sub:
                    chains.append(sub)
        raw = ImageCollector.raw_message(event)
        if raw is not None:
            chains.append(raw)
        return chains

    @staticmethod
    def _flatten(values: list, depth: int) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for value in values or []:
            if isinstance(value, (list, tuple)):
                out.extend(ImageCollector._flatten(list(value), depth))
            elif value not in (None, ""):
                out.append((str(value), depth))
        return out

    @staticmethod
    async def _fetch_forward(caller: Any, forward_id: str) -> Any:
        """用三种常见的参数名依次尝试 get_forward_msg。"""
        for key in ("id", "message_id", "forward_id"):
            try:
                payload = await caller("get_forward_msg", **{key: forward_id})
            except Exception:  # noqa: BLE001
                payload = None
            if payload:
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    return payload["data"]
                return payload
        return None

    @staticmethod
    def _is_image(component: Any) -> bool:
        """判断消息组件是否为图片（组件类可能来自不同版本的 AstrBot）。"""
        if isinstance(component, Image):
            return True
        return type(component).__name__ in ("Image", "Image_")

    @staticmethod
    def _is_at(component: Any) -> bool:
        """判断消息组件是否为 @（不同版本的组件包来源可能不同）。"""
        if isinstance(component, At):
            return True
        return type(component).__name__ == "At"

    @staticmethod
    def mention_texts(event: Any) -> list[str]:
        """消息链里真实 @ 的显示文本（形如 ``@铃``），用于精确剥离。

        只有在消息链里**确实存在 At 组件**时才返回文本：此时 ``message_str``
        里也带了同样的 ``@铃``，精确替换一次就能干净去掉。
        没有 At 组件时返回空列表，避免把文本里本来就该保留的 ``@`` 也剥掉。
        """
        chain = ImageCollector._messages(event)
        if not any(ImageCollector._is_at(component) for component in chain):
            return []
        texts: list[str] = []
        for component in chain:
            if not ImageCollector._is_at(component):
                continue
            qq = str(getattr(component, "qq", "") or "").strip()
            name = str(getattr(component, "name", "") or "").strip()
            if name:
                texts.append(f"@{name}")
            elif qq:
                # 没有显示名时按 AstrBot 拼进 message_str 的 `@QQ` 形式兜底
                texts.append(f"@{qq}")
        return texts

    @staticmethod
    def first_at_target(event: Any) -> Optional[str]:
        """取消息里第一个被 @ 的 QQ 号。

        跳过 @全体成员（all）和机器人自己：群里常有人先 @ 机器人再说话，
        若把自己的头像也算进去，每条消息都会多带一张无关图。
        """
        chain = ImageCollector._messages(event)
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
        for component in chain:
            if not ImageCollector._is_at(component):
                continue
            qq = str(getattr(component, "qq", "") or "").strip()
            if not qq or qq.lower() == "all" or not qq.isdigit():
                continue
            if self_id and qq == self_id:
                continue
            return qq
        return None

    async def avatar_path(self, event: Any, qq: Optional[str] = None) -> Optional[str]:
        """取第一个被 @ 的人的头像；开关关闭或采图优先级移除头像时直接返回 None。

        ``collect()`` 已经判断过开关，这里再判一次是为了让直接调用
        （命令、外部脚本）也不会绕过配置。
        """
        if not self.avatar_allowed():
            return None
        if qq is None:
            qq = self.first_at_target(event)
        if not qq:
            return None
        try:
            local = await self.downloader.materialize_image(AVATAR_URL_TEMPLATE.format(qq=qq))
        except Exception:  # noqa: BLE001
            logger.debug("[IMAGE] @头像下载失败：%s", qq, exc_info=True)
            return None
        return local or None

    # ------------------------------------------------------------------ 落地
    def _is_temporary(self, path: str) -> bool:
        """判断文件是否落在插件自己的 save_images 目录（临时副本）。

        用 ``resolve()`` 把相对路径、``..``、大小写差异都归一，避免同一个目录
        因写法不同而判成“不是临时文件”，导致临时图不被清理。
        """
        if not str(path or "").strip():
            return False
        try:
            candidate = Path(path).resolve()
            target = self.save_dir.resolve()
        except (OSError, ValueError):
            return False
        # Windows 不区分大小写；POSIX 用原样比较，避免把 /A 和 /a 当成同一个目录
        if os.name == "nt":
            return os.path.normcase(str(candidate.parent)) == os.path.normcase(str(target))
        return candidate.parent == target

    async def materialize(self, event: Any, ref: str) -> Optional[str]:
        """落地一张消息图片；普通下载失败时通过 OneBot 接口重新解析。"""
        candidates = [str(ref)]

        # AstrBot Image 经常同时包含临时 url 与 QQ file ID。历史功能不能只
        # 保留其中一个：URL 过期时仍应使用 file ID 让 OneBot 重新解析。
        def collect(chain) -> None:
            if not isinstance(chain, (list, tuple)):
                return
            for component in chain:
                if ImageCollector._is_image(component):
                    values = [str(getattr(component, key, "") or "") for key in ("url", "file", "path")]
                    if str(ref) in values:
                        candidates.extend(value for value in values if value)
                child = getattr(component, "chain", None)
                if child:
                    collect(child)

        collect(self._messages(event))
        candidates = list(dict.fromkeys(candidates))
        for candidate in candidates:
            local = await self.downloader.materialize_image(candidate)
            if local:
                return local
        caller = self._bot_caller(event)
        if caller is None:
            logger.warning(
                "[IMAGE] QQ 原图无法解析（没有可用的 OneBot 接口）: %s", str(ref)[:160]
            )
            return None
        # 依次尝试常见的 OneBot 参数名；QQ 侧不认识时立刻抛错，
        # 因此这里“全部试一遍”的实际代价很小，换来的是各实现都能兼容。
        attempts = []
        for candidate in candidates:
            attempts.extend((
                ("get_image", {"file": candidate}),
                ("get_image", {"file_id": candidate}),
                ("get_image", {"id": candidate}),
                ("get_file", {"file_id": candidate}),
            ))
        for action, params in attempts:
            try:
                payload = await caller(action, **params)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                payload = payload["data"]
            if not isinstance(payload, dict):
                continue
            for key in ("file", "path", "url"):
                resolved = payload.get(key)
                if resolved and str(resolved) not in candidates:
                    local = await self.downloader.materialize_image(str(resolved))
                    if local:
                        return local
        logger.warning(
            "[IMAGE] QQ 原图无法解析，已尝试 URL、本地引用和 OneBot 文件接口: %s",
            str(ref)[:160],
        )
        return None

    async def materialize_many(
        self,
        event: Any,
        refs: list[str],
        overrides: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> tuple[list[str | None], list[str]]:
        """按引用批量落地，保留“和输入一一对应”的槽位。

        返回的路径列表与 ``refs`` 等长：取到的是本地路径，**取不到的**是 ``None``；
        内容重复的那几张已经落地（去重只影响后续使用，不算失败）。
        以前这里把失败的项直接丢掉，调用方再拿“数量是否相等”判断成败，结果一张
        失败就报成“多数失败”，甚至把已经取到的图一起作废。
        """
        items = list(dict.fromkeys(str(r) for r in refs if str(r).strip()))
        fake_one = (overrides or {}).get("_materialize_message_image")
        # 覆盖项在 collect() 里的签名是 (event, ref)，这里要包一层再交给批量落地，
        # 否则同一个覆盖在两条路径上参数个数不同，会 TypeError 导致一张图都取不到。
        call_one = (lambda ref: fake_one(event, ref)) if fake_one else None
        landed, temporary, failed = await self._materialize_many(
            event, items, call_one, timeout=timeout, with_failures=True
        )
        # 只把“成功落地的项”和结果按顺序配对：
        # landed 里没有失败项，直接 zip(items, landed) 会让后面每一项都错位
        # （把 B 的路径配给 C），一旦哪天失败不再整体作废就会拿错图。
        failed_set = {str(x) for x in failed}
        ok_items = [str(item) for item in items if str(item) not in failed_set]
        mapping = dict(zip(ok_items, landed))
        return [mapping.get(str(ref)) for ref in refs], temporary, failed

    async def _materialize_many(
        self,
        event: Any,
        refs: list[str],
        materialize=None,
        timeout: Optional[float] = None,
        with_failures: bool = False,
    ):
        """批量落地，返回 (与输入同序的本地路径, 临时文件)。

        ``materialize`` 可传入兼容层的替身，默认用 :meth:`materialize`。
        ``timeout`` 给单张图设上限：APNG 一次要下很多张，某一张卡住不该把整条
        动图请求拖死；超时算“这张没取到”，由调用方决定怎么提示。
        ``with_failures=True`` 时再多返回一个“取不到的引用”列表（供 APNG 精确提示）。

        这里**不做内容去重**：重复算不算“有效输入”由调用方决定
        （单来源取图会去重；APNG 允许用户故意重复同一张图）。
        """
        materialize = materialize or (lambda ref: self.materialize(event, ref))
        paths: list[str] = []
        temporary: list[str] = []
        failed: list[str] = []
        for ref in dict.fromkeys(str(r) for r in refs if str(r).strip()):
            try:
                if timeout:
                    local = await asyncio.wait_for(materialize(ref), timeout=timeout)
                else:
                    local = await materialize(ref)
            except asyncio.TimeoutError:
                logger.warning("[neko_draw] 取图超时（%ss）：%s", timeout, ref[:160])
                local = None
            except Exception:  # noqa: BLE001
                logger.warning("[neko_draw] 取图失败：%s", ref[:160], exc_info=True)
                local = None
            if not local:
                failed.append(str(ref))
                continue
            paths.append(local)
            if self._is_temporary(local):
                temporary.append(local)
        if with_failures:
            return paths, temporary, failed
        return paths, temporary

    # ------------------------------------------------------------------ 覆盖项
    # 兼容层可以替换单个解析器（旧接口名 → 替身）。生图、扩写/提取、APNG
    # 三处都从这里取，避免每个调用方各自抄一份“该替换哪几个”的清单。
    OVERRIDE_KEYS = (
        "_extract_all_image_urls",
        "_extract_image_urls",
        "_extract_quoted_image_urls",
        "_extract_forward_image_urls",
        "_extract_at_avatar_url",
        "_materialize_message_image",
    )

    @classmethod
    def overrides_from(cls, source: Optional[dict]) -> dict:
        """从共享覆盖表里挑出取图相关的项（None 表示不替换）。"""
        source = source or {}
        return {key: source[key] for key in cls.OVERRIDE_KEYS if source.get(key) is not None}

    # ------------------------------------------------------------------ 主入口
    async def collect(
        self, event: Any, priority: Any = None, overrides: Optional[dict] = None
    ) -> CollectedImages:
        """按优先级取图：命中第一个有图的来源后，**只看这个来源**。

        ``overrides`` 用于兼容层替换单个解析器（旧接口名 → 替身）。
        这些替身只在主流程通过 ``app.overrides`` 显式传入时生效，
        平时为空，走下面这套真实实现。
        """
        overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        fake_all = overrides.get("_extract_all_image_urls")
        fake_one = overrides.get("_materialize_message_image")
        fake_message = overrides.get("_extract_image_urls")
        fake_quoted = overrides.get("_extract_quoted_image_urls")
        fake_forward = overrides.get("_extract_forward_image_urls")
        fake_avatar = overrides.get("_extract_at_avatar_url")

        async def refs_of(source: str) -> list[str]:
            if source == SOURCE_MESSAGE:
                return fake_message(event) if fake_message else self.message_refs(event)
            if source == SOURCE_QUOTED:
                return await fake_quoted(event) if fake_quoted else await self.quoted_refs(event)
            if source == SOURCE_FORWARD:
                return await fake_forward(event) if fake_forward else await self.forward_refs(event)
            return []

        async def materialize(ref: str) -> Optional[str]:
            if fake_one:
                return await fake_one(event, ref)
            return await self.materialize(event, ref)

        for source in self.priority(priority):
            if source == SOURCE_AVATAR:
                if not self.avatar_allowed(priority):
                    continue
                path = await fake_avatar(event) if fake_avatar else await self.avatar_path(event)
                if path:
                    temporary = [path] if self._is_temporary(path) else []
                    return CollectedImages(paths=[path], source=SOURCE_AVATAR, temporary=temporary)
                continue
            if fake_all is not None:
                # 兼容层给了“全部来源”的结果：整体当成 message 来源处理一次
                if source != SOURCE_MESSAGE:
                    continue
                refs = await fake_all(event)
            else:
                refs = await refs_of(source)
            if not refs:
                continue
            landed, temporary = await self._materialize_many(event, refs, materialize)
            # 单来源取图（生图 / 扩写 / 提取）：同一张图被多个解析器拿到只留一份。
            # APNG 走的是 materialize_many，那里不做内容去重——用户可以有意重复同一张图。
            paths, seen_digests = [], set()
            for path in landed:
                digest = await asyncio.to_thread(image_content_digest, path)
                if digest and digest in seen_digests:
                    if self._is_temporary(path) and path not in paths:
                        try:
                            Path(path).unlink(missing_ok=True)
                        except OSError:
                            pass
                    continue
                if digest:
                    seen_digests.add(digest)
                paths.append(path)
            if paths:
                kept_temporary = [path for path in temporary if path in paths]
                return CollectedImages(paths=paths, source=source, temporary=kept_temporary)
        return CollectedImages()

    def primary_refs(self, event: Any) -> list[str]:
        """本条消息链里明确出现的图片引用。

        保留是为了兼容旧接口；APNG 现在按 ``collect_all_by_source`` 的来源判断
        主图，不再用“引用字符串是否出现在消息链里”这种容易撞名的办法。
        """
        return list(dict.fromkeys(
            str(ref) for ref in self.message_refs(event) if str(ref).strip()
        ))

    async def collect_all(
        self, event: Any, overrides: Optional[dict] = None
    ) -> list[str]:
        """取全部来源的图片引用（去重、保序），用于 APNG 这类“每张都是输入”的场景。

        与 :meth:`collect` 的区别：这里不做“取到即停”，消息图、引用图、
        合并转发图会全部合并，用户发了什么就是什么。
        ``overrides`` 用于兼容层替换单个解析器（例如测试里的 ``_extract_all_image_urls``）。
        """
        sources = await self.collect_all_by_source(event, overrides)
        return list(sources)

    async def collect_all_by_source(
        self, event: Any, overrides: Optional[dict] = None
    ) -> dict[str, str]:
        """取全部来源的图片引用，并记下每张图**来自哪个来源**。

        APNG 用它区分“用户明确发的图”和“补充解析器顺带找到的图”：
        同名文件里，前者才是用户真正提交的那张。返回 ``{引用: 来源}``。

        ``_extract_all_image_urls`` 是一个“合并了所有来源”的旧接口，
        单看它的返回值分不清哪张来自引用、哪张来自转发。所以这里先跑它拿到全集，
        再用本条消息的真实图片标记 ``message``，其余引用归到“非本条消息”，
        这样“补充解析器多给的副本”才不会被误当成用户发的。
        """
        overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        fake_all = overrides.get("_extract_all_image_urls")
        if fake_all is not None:
            urls = fake_all(event)
            urls = await urls if hasattr(urls, "__await__") else urls
            refs = [str(ref).strip() for ref in (urls or []) if str(ref).strip()]
            # 用真实消息链判断“哪些确实是用户在本条消息里发的”
            real_message_refs = set(self.message_refs(event))
            result: dict[str, str] = {}
            for ref in refs:
                # 其余归到“引用”来源只是为了标记“不是本条消息里的”，
                # APNG 只用它区分“用户明确发的 / 补充解析器多给的”。
                result.setdefault(
                    ref, SOURCE_MESSAGE if ref in real_message_refs else SOURCE_QUOTED
                )
            return result
        fake_message = overrides.get("_extract_image_urls")
        fake_quoted = overrides.get("_extract_quoted_image_urls")
        fake_forward = overrides.get("_extract_forward_image_urls")
        sources: dict[str, str] = {}
        for source, getter, fake in (
            (SOURCE_MESSAGE, self.message_refs, fake_message),
            (SOURCE_QUOTED, self.quoted_refs, fake_quoted),
            (SOURCE_FORWARD, self.forward_refs, fake_forward),
        ):
            refs = fake(event) if fake else getter(event)
            if hasattr(refs, "__await__"):
                refs = await refs
            for ref in refs or []:
                text = str(ref).strip()
                if text:
                    sources.setdefault(text, source)
        return sources
