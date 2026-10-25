# -*- coding: utf-8 -*-
"""猫娘画图 —— 支持 APNG 制作的多提供商图片生成 AstrBot 插件。

这个文件只做三件事：
1. 向 AstrBot 声明插件本体与指令；
2. 读取配置、建立数据目录；
3. 把消息、指令、WebUI 转交给 ``core.app`` 里装配好的服务。

业务逻辑请看 ``core/flow.py``（消息主流程）、``core/handler.py``（出图）、
``core/text_tools.py``（扩写/提取）、``core/apng.py``（动图）。
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
# 组件类在这里导入供兼容：
#   1. 旧代码 / 外部脚本 ``from .main import Image``；
#   2. 测试通过 ``plugin_module.Image`` 构造消息链。
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig

from .core.apng_history import ApngHistoryStore
from .core.app import App, migrate_legacy_messages, reset_preprocess_once
from .core.commands import command_router
from .core.history import HistoryStore
from .core.prompt_manager import PromptManager
from .core.text_utils import (
    DEFAULT_LLM_TAG_PROMPT,
    detect_tag_refusal,
    strip_tag_noise,
)
# 兼容旧引用保留：这三个符号仍在 text_utils 里，供外部脚本 / 其他插件导入
from .core.whitelist import WhitelistGuard
from .webui import WebUIBridge

PLUGIN_DATA_DIR = "astrbot_plugin_neko_draw"

# At / Image / Plain / Reply 在这里导入，是为了兼容旧代码的
# ``from .main import Image`` 写法（msg 渲染会用到）。
#
# 需要绑定实例转发器的旧接口：测试 / 脚本会直接替换并断言它们
FORWARDER_METHODS = (
    "_extract_image_urls", "_extract_quoted_image_urls", "_extract_forward_image_urls",
    "_extract_all_image_urls", "_extract_at_avatar_url", "_first_at_target",
    "_avatar_allowed", "_materialize_message_image", "_command_llm",
    "_should_decompose", "_decompose_tags", "_handle_apng", "_send_apng_file",
    "_spawn_pending_delivery", "_send_pending_images", "_record_llm_call",
    "_notify_event", "_save_pending_record", "_deliver_pending",
)

# 这些旧方法名允许被外部替换（替换后主流程直接调用替换值）
# 服务属性 → runtime 上对应的字段名
SERVICE_ATTRS = {
    "providers": "providers",
    "templates": "templates",
    "prompt_manager": "prompt_manager",
    "expand_store": "expand_store",
    "extract_store": "extract_store",
    "whitelist_guard": "whitelist_guard",
    "downloader": "downloader",
    "handler": "handler",
    "output": "output",
    "rate_limiter": "rate_limiter",
    "llm_history": "llm_history",
}

COMPAT_OVERRIDES = frozenset({
    "_extract_image_urls", "_extract_quoted_image_urls", "_extract_forward_image_urls",
    "_extract_all_image_urls", "_extract_at_avatar_url", "_first_at_target",
    "_avatar_allowed", "_materialize_message_image", "_command_llm",
    "_should_decompose", "_decompose_tags", "_record_llm_call",
    "_spawn_pending_delivery", "_send_pending_images", "_handle_apng",
    "_notify_event",
})
PLUGIN_DIR = Path(__file__).parent

# 兼容旧引用：外部（测试 / 其他插件）可能从这里导入这几个符号
__all__ = [
    "NekoDrawPlugin",
    "DEFAULT_LLM_TAG_PROMPT",
    "detect_tag_refusal",
    "strip_tag_noise",
]


class NekoDrawPlugin(Star):
    """多提供商图片生成插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.conf = config
        migrate_legacy_messages(self.conf)

        # 数据目录
        self.data_dir = StarTools.get_data_dir(PLUGIN_DATA_DIR)
        self.save_dir = self.data_dir / "save_images"
        self.save_dir.mkdir(parents=True, exist_ok=True)

        # 3.9.2：拆解 tag 已删除，生图预处理改由「扩写」承担，升级时统一关闭
        reset_preprocess_once(self.conf, self.data_dir)

        # 先读取命令动态维护的数据，非破坏性地补充到 WebUI 配置视图
        self._sync_command_data()

        self.history_store = HistoryStore(self.data_dir)
        self.apng_history_store = ApngHistoryStore(self.data_dir)

        # 全部运行期服务由 App 统一装配
        self.app = App(
            context=context,
            config=self.conf,
            data_dir=self.data_dir,
            save_dir=self.save_dir,
            plugin_dir=PLUGIN_DIR,
            history=self.history_store,
            apng_history=self.apng_history_store,
        )

        self.webui_bridge = WebUIBridge(
            context=context,
            config=self.conf,
            history_store=self.history_store,
            apng_history_store=self.apng_history_store,
            data_dir=self.data_dir,
            plugin_dir=PLUGIN_DIR,
            on_config_saved=self._reload_model_runtime,
            recall_handler=self._recall_history_record,
            command_stores=self._command_stores,
            llm_history=lambda: self.app.runtime.llm_history,
        )
        self.webui_bridge.register_routes()
        self._bind_compat_forwarders()

    def _bind_compat_forwarders(self) -> None:
        """把旧接口绑定成实例属性，方便测试 / 脚本直接替换并断言。

        转发器语义：先看 ``app.overrides``（主流程也会看），没有就走 core 实现。
        """
        for name in FORWARDER_METHODS:
            if name in type(self).__dict__:
                continue
            forwarder = self._make_forwarder(name)
            if forwarder is not None:
                try:
                    setattr(type(self), name, forwarder)
                except Exception:  # noqa: BLE001
                    continue

    def _make_forwarder(self, name: str):
        """为旧接口生成“直接调用 core 实现”的绑定方法。

        覆盖（``plugin._xxx = fake``）由 ``__setattr__`` 写进 ``app.overrides``，
        主流程读取 overrides 生效；这里返回的绑定方法只负责“没有覆盖时”的行为。
        """
        core = self._core_compat_impl(name)
        if core is None:
            return None
        return core

    def _core_compat_impl(self, name: str):
        """返回某个旧接口对应的 core 实现（未绑定的函数）。"""
        app = self.__dict__.get("app")
        if app is None:
            return None
        mapping = {
            "_extract_image_urls": lambda: app.collector.message_refs,
            "_extract_quoted_image_urls": lambda: app.collector.quoted_refs,
            "_extract_forward_image_urls": lambda: app.collector.forward_refs,
            "_extract_all_image_urls": lambda: (
                lambda event, overrides=None: app.collector.collect_all(event)
            ),
            "_extract_at_avatar_url": lambda: app.collector.avatar_path,
            "_first_at_target": lambda: app.collector.first_at_target,
            "_avatar_allowed": lambda: app.collector.avatar_allowed,
            "_materialize_message_image": lambda: app.collector.materialize,
            "_command_llm": lambda: app.text_tools._command_llm,
            "_should_decompose": lambda: app.text_tools.should_preprocess,
            "_decompose_tags": lambda: app.text_tools.preprocess,
            "_handle_apng": lambda: app.apng.run_as_result,
            "_spawn_pending_delivery": lambda: NekoDrawPlugin._spawn_pending_delivery,
            "_send_pending_images": lambda: NekoDrawPlugin._send_pending_images,
            "_record_llm_call": lambda: NekoDrawPlugin._record_llm_call,
            "_save_pending_record": lambda: getattr(NekoDrawPlugin, "_save_pending_record"),
            "_deliver_pending": lambda: getattr(NekoDrawPlugin, "_deliver_pending"),
            "_notify_event": lambda: NekoDrawPlugin._notify_event,
        }
        factory = mapping.get(name)
        return factory() if factory else None

    # ------------------------------------------------------------------ 初始化
    def _sync_command_data(self) -> None:
        """把命令动态维护的预设 / 白名单补齐到内存配置，供 WebUI 显示与删除。

        以磁盘原始 config 为基底“只增不覆盖”，启动时不 save_config，
        避免版本更新后改写用户配置文件。
        """
        try:
            prompts = PromptManager(self.conf.get("prompt", []), self.data_dir, config=self.conf)
            whitelist = WhitelistGuard(
                enabled=bool(self.conf.get("whitelist_enabled", False)),
                group_whitelist=self.conf.get("group_whitelist", []),
                user_whitelist=self.conf.get("user_whitelist", []),
                data_dir=self.data_dir,
                config=self.conf,
            )
            self.conf["prompt"] = prompts.promote_to_config(self.conf.get("prompt", []))
            self.conf["group_whitelist"] = sorted(whitelist.groups)
            self.conf["user_whitelist"] = sorted(whitelist.users)
        except Exception:  # noqa: BLE001
            logger.warning("[neko_draw] 同步命令数据失败，继续使用配置文件内容", exc_info=True)

    def _command_stores(self) -> dict:
        return self.app.command_stores()

    # ---- 服务属性转发（读 runtime，写也写 runtime）
    @property
    def providers(self):
        return self.app.runtime.providers

    @property
    def templates(self):
        return self.app.runtime.templates

    @property
    def prompt_manager(self):
        return self.app.runtime.prompt_manager

    @property
    def expand_store(self):
        return self.app.runtime.expand_store

    @property
    def extract_store(self):
        return self.app.runtime.extract_store

    @property
    def whitelist_guard(self):
        return self.app.runtime.whitelist_guard

    @property
    def downloader(self):
        return self.app.runtime.downloader

    @property
    def handler(self):
        return self.app.runtime.handler

    @property
    def output(self):
        return self.app.runtime.output

    @property
    def rate_limiter(self):
        return self.app.runtime.rate_limiter

    @property
    def llm_history(self):
        return self.app.runtime.llm_history

    def __setattr__(self, name, value):
        """旧接口的赋值同时写“实例属性”和“app.overrides”。

        实例属性让测试 / 外部脚本能直接断言 ``plugin._xxx``；
        app.overrides 让主流程（flow / text_tools）读到同一个替身。
        """
        app = self.__dict__.get("app")
        if app is not None:
            if name in COMPAT_OVERRIDES:
                app.overrides[name] = value
                object.__setattr__(self, name, value)
                return
            if name == "history_store":
                app.history = value
                super().__setattr__(name, value)
                return
            if name in SERVICE_ATTRS and "runtime" in app.__dict__:
                setattr(app.runtime, name, value)
                try:
                    if name == "prompt_manager":
                        app.text_tools.prompt_manager = value
                except Exception:  # noqa: BLE001
                    pass
                return
        super().__setattr__(name, value)

    async def _reload_model_runtime(self) -> None:
        await self.app.reload()

    # ------------------------------------------------------------------ 消息入口
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_message(self, event: AstrMessageEvent) -> AsyncGenerator:
        """绘图命令消息入口。"""
        # 记一笔“在途请求”：生成过程中如果有人在面板保存配置触发重载，
        # 旧连接要等这条消息处理完再关，否则会打断正在生成的图。
        self.app.enter_request()
        try:
            async for result in self.app.flow.handle(event):
                yield result
        finally:
            await self.app.exit_request()

    # ------------------------------------------------------------------ 历史找回
    async def _recall_history_record(self, record_id: int, deliver: bool = True) -> dict:
        return await self.app.pending.recall(record_id, deliver)

    # ------------------------------------------------------------------ 兼容层
    # 旧版本把这些能力写在插件类上。为了不破坏外部脚本与既有测试，
    # 这里保留同名入口：读到的属性优先走 ``overrides``（可直接 monkeypatch），
    # 没被替换时转交给 core 里真正的实现。
    #
    # 这样主流程只需要在调用处问一句 ``self.app.overrides.get(name)``，
    # 不必为了兼容再复制一套逻辑。

    def _compat_get(self, name, fallback):
        """取实际要用的实现：被替换过用替换值，否则用 core 的实现。

        主流程（flow / text_tools）读的是同一份 ``app.overrides``，所以外部替换
        ``plugin._xxx`` 后行为一致。兼容入口本身不会再被写回 overrides
        （``__setattr__`` 只在赋值时记录），因此不存在自递归。
        """
        app = self.__dict__.get("app")
        overrides = app.__dict__.get("overrides") if app is not None else None
        if isinstance(overrides, dict):
            override = overrides.get(name)
            if override is not None and override is not fallback:
                return override
        return fallback

    @property
    def _pending_deliveries(self) -> set:
        """旧版公开属性：当前在后台等待补发的任务集合（只读视图）。"""
        return self.app.pending._tasks

    # ---- 取图
    def _extract_image_urls(self, event):
        return self._compat_get("_extract_image_urls", self.app.collector.message_refs)(event)

    async def _extract_quoted_image_urls(self, event):
        return await self._compat_get("_extract_quoted_image_urls", self.app.collector.quoted_refs)(event)

    async def _extract_forward_image_urls(self, event):
        return await self._compat_get("_extract_forward_image_urls", self.app.collector.forward_refs)(event)

    async def _extract_all_image_urls(self, event):
        return await self._compat_get("_extract_all_image_urls", self.app.collector.collect_all)(event)

    async def _extract_at_avatar_url(self, event):
        return await self._compat_get("_extract_at_avatar_url", self.app.collector.avatar_path)(event)

    def _first_at_target(self, event):
        return self.app.collector.first_at_target(event)

    def _avatar_allowed(self, priority=None):
        return self._compat_get("_avatar_allowed", self.app.collector.avatar_allowed)(priority)

    async def _materialize_message_image(self, event, ref):
        return await self._compat_get(
            "_materialize_message_image", self.app.collector.materialize
        )(event, ref)

    # ---- LLM
    async def _command_llm(self, event, **kwargs):
        return await self._compat_get("_command_llm", self.app.text_tools._command_llm)(event, **kwargs)

    async def _handle_command_section(self, event, section, text):
        """扩写/提取：命中返回纯文本，未命中返回 None。"""
        outcome = await self.app.text_tools.run(event, section, text)
        return None if outcome is None else outcome.text

    def _should_decompose(self, preset=None):
        return self._compat_get("_should_decompose", self.app.text_tools.should_preprocess)(preset)

    def _decompose_tags(self, user_text, event, preset=None):
        return self._compat_get("_decompose_tags", self.app.text_tools.preprocess)(
            user_text, event, preset
        )

    def _record_llm_call(self, event, **kwargs):
        """旧签名兼容：从关键字参数里还原一次调用记录。"""
        from .core.text_tools import TextToolResult

        outcome = TextToolResult(
            section=str(kwargs.get("section", "") or ""),
            body=str(kwargs.get("body", "") or ""),
            template=str(kwargs.get("template", "") or ""),
            system_prompt=str(kwargs.get("system_prompt", "") or ""),
            model=str(kwargs.get("model", "") or ""),
            duration_ms=float(kwargs.get("duration_ms") or 0),
            image_count=int(kwargs.get("image_count") or 0),
            image_source=str(kwargs.get("image_source", "") or ""),
            reason=str(kwargs.get("reason", "") or ""),
            meta={"provider_id": str(kwargs.get("provider_id", "") or "")},
        )
        self.app.record_llm_call(event, outcome, kwargs.get("item") or {}, kwargs.get("result") or "")

    def _vision_prompt(self, template, image_paths):
        from .core.llm_gateway import vision_prompt

        return vision_prompt(template, image_paths)

    # ---- 排队补发
    def _spawn_pending_delivery(
        self,
        event,
        text,
        result,
        gen_start,
        *,
        decompose_requested: bool = False,
        tags: str = "",
        request=None,
        record_id=None,
    ):
        """旧签名兼容：把排队任务交给后台补发（主流程已通过 forwarder 处理覆盖）。"""
        from .core.objects import RequestContext, build_request_context

        ctx = (
            RequestContext.from_dict(request) if isinstance(request, dict)
            else build_request_context(
                result,
                trigger=getattr(result, "trigger", ""),
                apng_choice=str(getattr(result, "apng", "") or ""),
                decompose_requested=decompose_requested,
                tags=tags,
                umo=str(getattr(event, "unified_msg_origin", "") or ""),
            )
        )
        return self.app.pending.spawn(
            self.app.pending.deliver(event, text, result, gen_start, ctx, record_id=record_id)
        )

    def _save_pending_record(self, event, text, result, ctx, gen_start):
        """排队超时先写一条「排队中」记录，返回记录 ID（旧接口）。"""
        return self.app.pending.save_pending_record(event, text, result, ctx, gen_start)

    async def _deliver_pending(
        self,
        event,
        text,
        result,
        gen_start,
        *,
        decompose_requested: bool = False,
        tags: str = "",
        request=None,
        record_id=None,
    ):
        """后台等待排队任务结果并补发（旧接口）。"""
        from .core.objects import RequestContext, build_request_context, resolve_apng_choice

        if isinstance(request, dict) and request:
            ctx = RequestContext.from_dict(request)
        else:
            trigger = getattr(result, "trigger", "") or ""
            preset = self.prompt_manager.get_by_trigger(trigger) if trigger else None
            ctx = build_request_context(
                result,
                trigger=trigger,
                # 没有显式选择时沿用预设的 APNG 三态（与主流程同一条规则）
                apng_choice=resolve_apng_choice(
                    getattr(result, "apng", ""), (preset or {}).get("apng", "")
                ),
                decompose_requested=decompose_requested,
                tags=tags,
                umo=str(getattr(event, "unified_msg_origin", "") or ""),
            )
        return await self.app.pending.deliver(
            event, text, result, gen_start, ctx, record_id=record_id
        )

    async def _send_pending_images(
        self, event, image_paths, apng_enabled=None, reference_images=None
    ):
        """旧接口：按 APNG 开关包装后发出（主流程与后台补发共用）。

        ``reference_images`` 是用户当初发的那几张图的临时副本。当前
        ``_wrap_apng`` 并不消费它，这里保持一致地传下去即可；调用方负责在
        包装期间保活、结束后清理。
        """
        sent = await self.app.sender.wrap_apng(
            list(image_paths), apng_enabled, reference_images
        )
        try:
            await self.app.sender.send_images(event, sent)
        finally:
            await self.app.sender.cleanup_transient(sent, list(image_paths))

    # ---- APNG
    def _parse_apng_options(self, text):
        return self.app.apng.parse_options(text)

    async def _send_apng_file(self, event, output):
        from .core.image_sender import PROFILE_APNG

        return await self.app.sender.send_images(event, [output], profile=PROFILE_APNG)

    async def _handle_apng(self, event):
        return await self._compat_get("_handle_apng", self.app.apng.run_as_result)(event)

    async def _redeliver_to_session(self, umo, image_paths, apng_enabled=None):
        sent = await self.app.sender.wrap_apng(list(image_paths), apng_enabled, None)
        try:
            return await self.app.sender.send_images_to_session(self.context, umo, sent)
        finally:
            await self.app.sender.cleanup_transient(sent, list(image_paths))

    # ---- 杂项
    def _strip_command(self, text, commands):
        """旧版工具：去掉命令前缀。新代码直接用 ``core.text_utils.split_command``。"""
        from .core.text_utils import split_command

        return split_command(text, commands)

    def _should_at_sender(self):
        return self.app.flow._should_at_sender()

    def _build_request_context(self, text, result, *, decompose_requested=False, tags="", request=None):
        """旧签名兼容：汇总一次请求的选项快照。"""
        from .core.objects import RequestContext, build_request_context, resolve_apng_choice

        if isinstance(request, dict) and request:
            return RequestContext.from_dict(request).as_dict()
        preset_trigger = getattr(result, "preset_trigger", "") or getattr(result, "trigger", "")
        preset = self.prompt_manager.get_by_trigger(preset_trigger) if preset_trigger else None
        ctx = build_request_context(
            result,
            trigger=getattr(result, "trigger", "") or preset_trigger,
            apng_choice=resolve_apng_choice(
                getattr(result, "apng", ""), (preset or {}).get("apng", "")
            ),
            decompose_requested=decompose_requested,
            decompose_status="success" if tags else ("fallback" if decompose_requested else "disabled"),
            tags=tags,
            umo=str(getattr(result, "umo", "") or ""),
        )
        return ctx.as_dict()

    def _history_options(self, ctx=None, **extra):
        """旧签名兼容：拼装写入历史的选项快照。"""
        return self._as_context(ctx).history_options(**extra)

    @staticmethod
    def _as_context(ctx):
        """把 dict / None 统一成 RequestContext。"""
        from .core.objects import RequestContext

        return ctx if isinstance(ctx, RequestContext) else RequestContext.from_dict(ctx or {})

    def _should_retain_history(self):
        return self.app.pending.should_retain_history()

    def _at_chain_result(self, event, sender, reply):
        return self.app.flow._at_chain_result(event, sender, reply)

    async def _notify_event(self, event, message: str) -> None:
        """开发 / 测试用的通知出口；主流程通过 app.overrides 读取。"""
        await self.app.flow._notify(event, message)

    # ------------------------------------------------------------------ 指令
    @filter.command_group("nc")
    def neko_config():
        """猫娘画图管理指令组。"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("h")
    async def config_help_command(self, event: AstrMessageEvent):
        async for item in command_router(self, "help", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("pa")
    async def add_prompt_command(self, event: AstrMessageEvent):
        """/nc pa <触发词> <提示词内容>"""
        async for item in command_router(self, "add_prompt", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("pd")
    async def del_prompt_command(self, event: AstrMessageEvent, trigger_word: str = ""):
        """/nc pd <触发词>"""
        async for item in command_router(self, "del_prompt", event, trigger_word):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("pl")
    async def list_prompts_command(self, event: AstrMessageEvent):
        """/nc pl"""
        async for item in command_router(self, "list_prompts", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("ps")
    async def prompt_details_command(self, event: AstrMessageEvent, trigger_word: str = ""):
        """/nc ps <触发词>"""
        async for item in command_router(self, "prompt_details", event, trigger_word):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("ea")
    async def add_expand_command(self, event: AstrMessageEvent):
        """/nc ea <触发词> <提示词模板>"""
        async for item in command_router(self, "add_expand", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("ed")
    async def del_expand_command(self, event: AstrMessageEvent, trigger_word: str = ""):
        """/nc ed <触发词>"""
        async for item in command_router(self, "del_expand", event, trigger_word):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("el")
    async def list_expand_command(self, event: AstrMessageEvent):
        """/nc el"""
        async for item in command_router(self, "list_expand", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("ta")
    async def add_extract_command(self, event: AstrMessageEvent):
        """/nc ta <触发词> <提示词模板>"""
        async for item in command_router(self, "add_extract", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("td")
    async def del_extract_command(self, event: AstrMessageEvent, trigger_word: str = ""):
        """/nc td <触发词>"""
        async for item in command_router(self, "del_extract", event, trigger_word):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("tl")
    async def list_extract_command(self, event: AstrMessageEvent):
        """/nc tl"""
        async for item in command_router(self, "list_extract", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("wa")
    async def add_whitelist_command(
        self, event: AstrMessageEvent, cmd_type: str = "", target_id: str = ""
    ):
        """/nc wa <u|g> <ID>"""
        async for item in command_router(self, "add_whitelist", event, cmd_type, target_id):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("wd")
    async def del_whitelist_command(
        self, event: AstrMessageEvent, cmd_type: str = "", target_id: str = ""
    ):
        """/nc wd <u|g> <ID>"""
        async for item in command_router(self, "del_whitelist", event, cmd_type, target_id):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("wl")
    async def list_whitelist_command(self, event: AstrMessageEvent):
        """/nc wl"""
        async for item in command_router(self, "list_whitelist", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("fs")
    async def set_first_frame_command(self, event: AstrMessageEvent):
        """/nc fs + 图片"""
        async for item in command_router(self, "set_first_frame", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("fv")
    async def view_first_frame_command(self, event: AstrMessageEvent):
        """/nc fv"""
        async for item in command_router(self, "view_first_frame", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("fr")
    async def reset_first_frame_command(self, event: AstrMessageEvent):
        """/nc fr"""
        async for item in command_router(self, "reset_first_frame", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("ao")
    async def enable_apng_command(self, event: AstrMessageEvent):
        """/nc ao"""
        async for item in command_router(self, "enable_apng", event):
            yield item

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("af")
    async def disable_apng_command(self, event: AstrMessageEvent):
        """/nc af"""
        async for item in command_router(self, "disable_apng", event):
            yield item

    # ------------------------------------------------------------------ 退出
    async def terminate(self):
        """卸载时取消后台补发任务并关闭所有网络会话。"""
        await self.app.shutdown()
