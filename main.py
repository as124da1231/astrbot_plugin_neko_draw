# -*- coding: utf-8 -*-
"""猫娘画图 —— 支持 APNG 展示的多提供商图片生成 AstrBot 插件。

主要功能：
- 预设提示词：<触发词> <提示词> --参数 值，{{user_text}} 占位符
- 命令：/猫娘添加 /猫娘删除 /猫娘列表 /猫娘提示词 /猫娘白名单添加 /猫娘白名单删除 /猫娘白名单列表
- 模型路由：消息带图 -> 编辑模型（bytedance/seedream-v5.0-pro/edit）
            消息无图 -> 文生图模型（bytedance/seedream-v5.0-pro）
            --model text|edit|完整模型名 可显式指定
"""
import asyncio
import random
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, File, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools
from astrbot.core import AstrBotConfig

from .core.output import OutputService
from .core.rate_limit import RateLimiter
from .core.parser import parse_prompt_message
from .core.downloader import Downloader
from .core.handler import DrawingHandler
from .core.history import HistoryStore
from .core.image_extract import extract_image_urls
from .core.providers import create_providers
from .core.prompt_manager import PromptManager
from .core.templates import TemplateManager
from .core.whitelist import WhitelistGuard
from .webui import WebUIBridge

PLUGIN_DATA_DIR = "astrbot_plugin_neko_draw"
PLUGIN_DIR = Path(__file__).parent


class NekoDrawPlugin(Star):
    """多提供商图片生成插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

        # 数据目录
        self.data_dir = StarTools.get_data_dir(PLUGIN_DATA_DIR)
        self.save_dir = self.data_dir / "save_images"
        self.save_dir.mkdir(parents=True, exist_ok=True)

        # 限流记录（内存存储，插件重启后清零）
        self.rate_limiter = RateLimiter(self.conf)

        # 预设提示词与白名单
        self.prompt_manager = PromptManager(
            self.conf.get("prompt", []), self.data_dir, config=self.conf
        )
        self.whitelist_guard = WhitelistGuard(
            enabled=bool(self.conf.get("whitelist_enabled", False)),
            group_whitelist=self.conf.get("group_whitelist", []),
            user_whitelist=self.conf.get("user_whitelist", []),
            data_dir=self.data_dir,
            config=self.conf,
        )

        # 非破坏性地把命令动态维护的预设/白名单补充到内存配置，供 WebUI 显示与删除。
        # 以磁盘原始 config 为基底“只增不覆盖”：用户已有项（含字符串旧预设、
        # __template_key、reference_image 等全部字段）原样保留，仅追加命令新增项；
        # 不在启动时 save_config 落盘，避免版本更新后改写用户配置文件。缺失的模板
        # 标识由前端渲染层与运行时按模板兜底。用户显式执行增删命令时才经 _save() 持久化。
        self.conf['prompt'] = self.prompt_manager.promote_to_config(self.conf.get('prompt', []))
        self.conf['group_whitelist'] = sorted(self.whitelist_guard.groups)
        self.conf['user_whitelist'] = sorted(self.whitelist_guard.users)

        # 网络组件
        proxy = self.conf.get("proxy") or None
        self.downloader = Downloader(self.save_dir, proxy=proxy)

        self.providers = create_providers(self.conf)
        self.wavespeed_client = self.providers['wavespeed']
        self.runninghub_client = self.providers['runninghub']
        self.openapi_client = self.providers['openapi']

        # 核心业务
        self.templates = TemplateManager(self.conf.get("model_templates", []))
        self.handler = DrawingHandler(
            templates=self.templates,
            default_text_model=self.conf.get("default_text_model", ""),
            default_edit_model=self.conf.get("default_edit_model", ""),
            prompt_manager=self.prompt_manager,
            whitelist_guard=self.whitelist_guard,
            downloader=self.downloader,
            providers=self.providers,
        )

        # 生成历史
        self.history_store = HistoryStore(self.data_dir)

        # WebUI 后端 API
        self.webui_bridge = WebUIBridge(
            context=context,
            config=self.conf,
            history_store=self.history_store,
            data_dir=self.data_dir,
            plugin_dir=PLUGIN_DIR,
        )
        self.webui_bridge.register_routes()

        # 图片外显金句
        self.output = OutputService(self.conf, self.data_dir, self.save_dir, PLUGIN_DIR)


    # ---------------- 工具 ----------------
    @staticmethod
    def _strip_command(text: str, commands: list[str]) -> Optional[str]:
        """去掉命令前缀（兼容带/与不带/），返回剩余部分。"""
        text = (text or "").strip()
        for cmd in commands:
            for prefix in (f"/{cmd}", cmd):
                if text == prefix:
                    return ""
                if text.startswith(prefix + " "):
                    return text[len(prefix):].strip()
        return None

    @staticmethod
    def _extract_image_urls(event: AstrMessageEvent) -> list[str]:
        """从消息链提取图片引用。

        覆盖：当前消息图片、引用（回复）消息中的图片、文件形式图片。
        """
        return extract_image_urls(
            event.get_messages(),
            image_types=(Image,),
            reply_types=(Reply,),
            file_types=(File,),
        )

    @staticmethod
    def _is_command_text(text: str) -> bool:
        """判断消息是否为插件自身命令（跳过绘图处理）。

        只拦截本插件注册的 /猫娘xxx 命令，其他 / 开头的消息
        允许作为绘图触发词（如 /杯子化）。
        """
        text = (text or "").strip()
        for cmd in (
            "猫娘添加",
            "猫娘删除",
            "猫娘列表",
            "猫娘提示词",
            "猫娘白名单添加",
            "猫娘白名单删除",
            "猫娘白名单列表",
        ):
            for prefix in (f"/{cmd}", cmd):
                if text == prefix or text.startswith(prefix + " "):
                    return True
        return False












    # ---------------- 预设提示词命令 ----------------
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("猫娘添加")
    async def add_prompt_command(self, event: AstrMessageEvent):
        """/猫娘添加 <触发词> <提示词内容>"""
        rest = self._strip_command(event.message_str, ["猫娘添加"])
        if rest is None:
            rest = ""
        parts = rest.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("❌ 格式错误：/猫娘添加 <触发词> <提示词内容>")
            return
        trigger, prompt_body = parts
        ok, msg = self.prompt_manager.add(trigger, prompt_body)
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("猫娘删除")
    async def del_prompt_command(self, event: AstrMessageEvent, trigger_word: str = ""):
        """/猫娘删除 <触发词>"""
        trigger_word = trigger_word.strip()
        if not trigger_word:
            yield event.plain_result("❌ 格式错误：/猫娘删除 <触发词>")
            return
        ok, msg = self.prompt_manager.delete(trigger_word)
        yield event.plain_result(f"{'🗑️' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("猫娘列表")
    async def list_prompts_command(self, event: AstrMessageEvent):
        """/猫娘列表"""
        triggers = self.prompt_manager.list_prompts()
        if not triggers:
            yield event.plain_result("📜 当前没有预设提示词。")
            return
        yield event.plain_result("📜 当前预设提示词列表：\n" + "、".join(triggers))

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("猫娘提示词")
    async def prompt_details_command(
        self, event: AstrMessageEvent, trigger_word: str = ""
    ):
        """/猫娘提示词 <触发词>"""
        trigger_word = trigger_word.strip()
        if not trigger_word:
            yield event.plain_result("❌ 格式错误：/猫娘提示词 <触发词>")
            return
        detail = self.prompt_manager.get_detail(trigger_word)
        if detail is None:
            yield event.plain_result(f"❌ 未找到提示词：「{trigger_word}」")
            return
        yield event.plain_result(f"📋 提示词详情：「{trigger_word}」\n{detail}")

    # ---------------- 白名单命令 ----------------
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("猫娘白名单添加")
    async def add_whitelist_command(
        self, event: AstrMessageEvent, cmd_type: str = "", target_id: str = ""
    ):
        """/猫娘白名单添加 <用户|群组> <ID>"""
        ok, msg = self.whitelist_guard.add(cmd_type, target_id)
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("猫娘白名单删除")
    async def del_whitelist_command(
        self, event: AstrMessageEvent, cmd_type: str = "", target_id: str = ""
    ):
        """/猫娘白名单删除 <用户|群组> <ID>"""
        ok, msg = self.whitelist_guard.delete(cmd_type, target_id)
        yield event.plain_result(f"{'🗑️' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @filter.command("猫娘白名单列表")
    async def list_whitelist_command(self, event: AstrMessageEvent):
        """/猫娘白名单列表"""
        yield event.plain_result(self.whitelist_guard.list_whitelist())

    # ---------------- 绘图消息入口 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_message(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator:
        """绘图命令消息入口。"""
        text = event.message_str.strip()
        if not text or self._is_command_text(text):
            return
        # 支持 /触发词 形式：去掉开头的 / 后匹配预设提示词
        if text.startswith("/"):
            text = text[1:].strip()
            if not text:
                return

        if not self.whitelist_guard.check(event.get_sender_id(), event.get_group_id()):
            return
        if parse_prompt_message(text, self.prompt_manager.get_all()) is None:
            return
        image_urls = self._extract_image_urls(event)

        # 预设风格参考图：匹配到预设时自动加入参考图，按该预设配置的位置排列
        preset_ref_result = self.prompt_manager.get_reference_image_for_text(text)
        if preset_ref_result is not None:
            preset_ref_raw, position = preset_ref_result
            preset_ref_path = self.output._resolve_file_ref(preset_ref_raw)
            if preset_ref_path is not None:
                preset_ref_str = str(preset_ref_path)
                if position == "after":
                    # 用户上传图在前，预设参考图在后
                    image_urls = image_urls + [preset_ref_str]
                else:
                    # 预设参考图在前（风格优先），用户上传图在后
                    image_urls = [preset_ref_str] + image_urls

        async def _progress(stage: str):
            """进度回调：开始生成时发送提示。"""
            if stage != "start":
                return
            if not self.conf.get("enable_drawing_message", True):
                return
            msg = str(self.conf.get("drawing_message", "")).strip()
            if msg:
                await event.send(event.plain_result(msg))

        # 单用户限流检查（通过则记录本次使用时间）
        reservation, triggered_rule, retry_after = self.rate_limiter.reserve(event.get_sender_id())
        if triggered_rule:
            window_desc = self.rate_limiter.format_window(triggered_rule["window_seconds"])
            max_count = triggered_rule["max_count"]
            msg_template = str(self.conf.get(
                "rate_limit_message",
                "🐱 绘图频率过高，{window}内最多{max_count}次，请{retry_after}秒后再试",
            ))
            try:
                msg = msg_template.format(window=window_desc, max_count=max_count, retry_after=retry_after)
            except (KeyError, IndexError, ValueError):
                msg = f"🐱 绘图频率过高，{window_desc}内最多{max_count}次，请{retry_after}秒后再试"
            yield event.chain_result([At(event.get_sender_id()), Plain(msg)])
            return

        _gen_start = time.time()
        result = None
        try:
            result = await self.handler.handle(
                text,
                image_urls,
                user_id=event.get_sender_id(),
                group_id=event.get_group_id(),
                progress_cb=_progress,
            )
        finally:
            self.rate_limiter.finish(reservation, bool(result and result.images))
        _gen_ms = (time.time() - _gen_start) * 1000

        if result.silent:
            return

        # 记录生成历史（成功和失败都记，静默忽略的不记）
        if result.model_template:
            try:
                self.history_store.add(
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
                    generation_time_ms=_gen_ms,
                )
            except Exception:  # noqa: BLE001
                logger.warning("[neko_draw] 历史记录写入失败", exc_info=True)

        if result.images:
            # 生成成功，记录限流使用（失败不扣次数）
            # 处理链第一步：APNG 动图包装（生成图 → APNG → 外显金句 → 合并转发）
            result_images = await asyncio.to_thread(self.output._wrap_apng, result.images)

            enable_forward = bool(self.conf.get("enable_forward_message", False))
            enable_summary = bool(self.conf.get("enable_image_summary", False))

            if enable_forward and enable_summary:
                # 两个都开：合并转发 + 图片外显金句同时生效
                quote = random.choice(self.output._image_summary_quotes)
                ok = await self.output._send_forward_message(
                    event, result_images, summary=quote
                )
                if not ok:
                    # 合并转发失败，回退普通图片+外显
                    ok2 = await self.output._send_image_with_summary(event, result_images)
                    if not ok2:
                        yield event.chain_result(
                            self.output._image_chain_with_at(event, result_images)
                        )
            elif enable_forward:
                # 只开合并转发
                ok = await self.output._send_forward_message(event, result_images)
                if not ok:
                    yield event.chain_result(
                        self.output._image_chain_with_at(event, result_images)
                    )
            elif enable_summary:
                # 只开图片外显金句
                ok = await self.output._send_image_with_summary(event, result_images)
                if not ok:
                    yield event.chain_result(
                        self.output._image_chain_with_at(event, result_images)
                    )
            else:
                # 普通图片发送
                yield event.chain_result(
                    self.output._image_chain_with_at(event, result_images)
                )
        elif result.text:
            yield event.plain_result(result.text)

    async def terminate(self):
        """卸载时关闭所有网络会话。"""
        for client in (
            self.wavespeed_client,
            self.runninghub_client,
            self.openapi_client,
            self.downloader,
        ):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
