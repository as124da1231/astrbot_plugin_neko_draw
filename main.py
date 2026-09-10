# -*- coding: utf-8 -*-
"""猫娘画图 —— 支持 APNG 制作的多提供商图片生成 AstrBot 插件。

主要功能：
- 预设提示词：<触发词> <提示词> --参数 值，{{user_text}} 占位符
- 绘图：nd；编辑：nde；APNG：apng；管理指令组：/nc
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

from .core.parser import parse_prompt_message
from .core.history import HistoryStore
from .core.apng_history import ApngHistoryStore
from .core.forward_images import extract_forward_ids, extract_payload_images
from .core.image_extract import extract_image_urls
from .core.image_dedupe import image_content_digest
from .core.prompt_manager import PromptManager
from .core.runtime import RuntimeServices
from .core.whitelist import WhitelistGuard
from .webui import WebUIBridge

PLUGIN_DATA_DIR = "astrbot_plugin_neko_draw"
PLUGIN_DIR = Path(__file__).parent


class NekoDrawPlugin(Star):
    """多提供商图片生成插件入口。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.conf = config
        # 仅迁移旧版内置默认文案；用户填写的其他自定义内容保持不变。
        drawing_notice = "小猫正在搓屏幕中/ᐠ - ˕ -マ Ⳋ📱"
        if self.conf.get("drawing_message") in {
            "🐱 猫娘正在画图，请稍候...", "小猫正在搓屏幕中"
        }:
            self.conf["drawing_message"] = drawing_notice
        if self.conf.get("apng_drawing_message") == "🐱 猫娘正在制作 APNG，请稍候...":
            self.conf["apng_drawing_message"] = drawing_notice

        # 数据目录
        self.data_dir = StarTools.get_data_dir(PLUGIN_DATA_DIR)
        self.save_dir = self.data_dir / "save_images"
        self.save_dir.mkdir(parents=True, exist_ok=True)

        # 先读取命令动态维护的数据，并非破坏性地补充到 WebUI 配置视图。
        startup_prompts = PromptManager(
            self.conf.get("prompt", []), self.data_dir, config=self.conf
        )
        startup_whitelist = WhitelistGuard(
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
        self.conf['prompt'] = startup_prompts.promote_to_config(self.conf.get('prompt', []))
        self.conf['group_whitelist'] = sorted(startup_whitelist.groups)
        self.conf['user_whitelist'] = sorted(startup_whitelist.users)

        # 所有配置驱动的服务由一个运行时容器统一创建和替换。
        self._runtime = RuntimeServices.build(
            self.conf,
            context=self.context,
            data_dir=self.data_dir,
            save_dir=self.save_dir,
            plugin_dir=PLUGIN_DIR,
        )
        self._install_runtime(self._runtime)

        # 生成历史
        self.history_store = HistoryStore(self.data_dir)
        self.apng_history_store = ApngHistoryStore(self.data_dir)

        # WebUI 后端 API
        self.webui_bridge = WebUIBridge(
            context=context,
            config=self.conf,
            history_store=self.history_store,
            apng_history_store=self.apng_history_store,
            data_dir=self.data_dir,
            plugin_dir=PLUGIN_DIR,
            on_config_saved=self._reload_model_runtime,
        )
        self.webui_bridge.register_routes()

    def _install_runtime(self, runtime: RuntimeServices) -> None:
        """Expose stable service names used by the event and command adapters."""
        self.providers = runtime.providers
        self.templates = runtime.templates
        self.prompt_manager = runtime.prompt_manager
        self.whitelist_guard = runtime.whitelist_guard
        self.downloader = runtime.downloader
        self.handler = runtime.handler
        self.output = runtime.output
        self.rate_limiter = runtime.rate_limiter

    async def _reload_model_runtime(self) -> None:
        """保存 WebUI 后原子替换运行组件，让所有设置无需重启即可生效。"""
        old_runtime = self._runtime
        new_runtime = RuntimeServices.build(
            self.conf,
            context=self.context,
            data_dir=self.data_dir,
            save_dir=self.save_dir,
            plugin_dir=PLUGIN_DIR,
            previous=old_runtime,
        )
        self._runtime = new_runtime
        self._install_runtime(new_runtime)
        await old_runtime.close()
        await asyncio.to_thread(self.history_store.enforce_limit, max(0, int(self.conf.get("drawing_history_limit", 200))))
        await asyncio.to_thread(self.apng_history_store.enforce_limit, max(0, int(self.conf.get("apng_history_limit", 200))))


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
    def _raw_message(event: AstrMessageEvent):
        message_obj = getattr(event, "message_obj", None)
        return getattr(message_obj, "raw_message", None)

    async def _extract_forward_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """展开 QQ/OneBot 合并转发，并按节点顺序收集图片。"""
        sources = [event.get_messages(), self._raw_message(event)]
        queue = []
        for source in sources:
            queue.extend(extract_forward_ids(source))
        if not queue:
            return []
        bot = getattr(event, "bot", None)
        caller = getattr(getattr(bot, "api", None), "call_action", None)
        if caller is None:
            caller = getattr(bot, "call_action", None)
        if caller is None:
            return []

        images: list[str] = []
        visited: set[str] = set()
        max_depth = 3
        current = [(value, 0) for value in queue]
        while current:
            forward_id, depth = current.pop(0)
            if forward_id in visited or depth > max_depth:
                continue
            visited.add(forward_id)
            payload = None
            for key in ("id", "message_id", "forward_id"):
                try:
                    payload = await caller("get_forward_msg", **{key: forward_id})
                    if payload:
                        break
                except Exception:  # noqa: BLE001
                    payload = None
            if not payload:
                continue
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                payload = payload["data"]
            images.extend(extract_payload_images(payload))
            if depth < max_depth:
                current.extend((value, depth + 1) for value in extract_forward_ids(payload))
        return images

    async def _extract_all_image_urls(self, event: AstrMessageEvent) -> list[str]:
        urls = self._extract_image_urls(event)
        # AstrBot 4.28+ 自带的引用消息解析器能通过 reply id 重新获取原消息，
        # 处理 Reply.chain 为空、base64:// 和 QQ file ID 等平台差异。
        try:
            from astrbot.core.utils.quoted_message import extract_quoted_message_images
            urls.extend(await extract_quoted_message_images(event))
        except (ImportError, AttributeError):
            pass
        except Exception:  # noqa: BLE001
            logger.debug("[IMAGE] AstrBot 引用图片解析器未返回结果", exc_info=True)
        urls.extend(await self._extract_forward_image_urls(event))
        return list(dict.fromkeys(str(url) for url in urls if str(url).strip()))

    async def _materialize_message_image(self, event: AstrMessageEvent, ref: str) -> Optional[str]:
        """落地消息图片；普通下载失败时通过 OneBot get_image 解析 QQ 文件 ID。"""
        candidates = [str(ref)]

        # AstrBot Image 经常同时包含临时 url 与 QQ file ID。历史功能不能只
        # 保留其中一个：URL 过期时仍应使用 file ID 让 OneBot 重新解析。
        def collect(chain) -> None:
            if not isinstance(chain, (list, tuple)):
                return
            for component in chain:
                if isinstance(component, Image):
                    values = [str(getattr(component, key, "") or "") for key in ("url", "file", "path")]
                    if str(ref) in values:
                        candidates.extend(value for value in values if value)
                child = getattr(component, "chain", None)
                if child:
                    collect(child)

        collect(event.get_messages())
        candidates = list(dict.fromkeys(candidates))
        for candidate in candidates:
            local = await self.downloader.materialize_image(candidate)
            if local:
                return local
        bot = getattr(event, "bot", None)
        caller = getattr(getattr(bot, "api", None), "call_action", None)
        if not callable(caller):
            return None
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
        logger.warning("[IMAGE] QQ 原图无法解析，已尝试 URL、本地引用和 OneBot 文件接口: %s", str(ref)[:160])
        return None

    @staticmethod
    def _is_command_text(text: str) -> bool:
        """判断消息是否为插件自身的斜杠命令。"""
        text = (text or "").strip()
        return (
            text == "/apng"
            or text.startswith("/apng ")
            or text == "/nc"
            or text.startswith("/nc ")
        )












    def _persist_config(self) -> None:
        if hasattr(self.conf, "save_config"):
            self.conf.save_config()

    def _parse_apng_options(self, text: str) -> tuple[int, int, int]:
        rest = NekoDrawPlugin._strip_command(text, ["apng"])
        parts = (rest or "").split()
        try:
            seconds = float(self.conf.get("apng_default_interval_seconds", 5))
            loop = int(self.conf.get("apng_loop", 0))
            first_seconds = int(self.conf.get("apng_first_frame_duration", 100)) / 1000
            index = 0
            if index < len(parts) and parts[index] not in {"--fd", "-fd"}:
                seconds = float(parts[index])
                index += 1
            if index < len(parts) and parts[index] not in {"--fd", "-fd"}:
                loop = int(parts[index])
                index += 1
            if index < len(parts):
                if parts[index] not in {"--fd", "-fd"} or index + 1 >= len(parts):
                    raise ValueError
                first_seconds = float(parts[index + 1])
                index += 2
            if index != len(parts):
                raise ValueError
        except ValueError as exc:
            raise ValueError("参数格式错误；用法：apng [间隔秒数] [循环次数] [--fd 首帧秒数]") from exc
        if not 0.1 <= seconds <= 60:
            raise ValueError("每帧间隔必须为 0.1～60 秒")
        if not 0 <= loop <= 100:
            raise ValueError("循环次数必须为 0～100，0 表示无限循环")
        if not 0.02 <= first_seconds <= 60:
            raise ValueError("首帧停留时间必须为 0.02～60 秒")
        return round(seconds * 1000), loop, round(first_seconds * 1000)

    async def _send_apng_file(self, event: AstrMessageEvent, output: str) -> None:
        """按 APNG 独立发送设置发送成品，失败时回退为普通图片。"""
        paths = [output]
        enable_forward = bool(self.conf.get("apng_enable_forward_message", False))
        enable_summary = bool(self.conf.get("apng_enable_image_summary", False))
        if enable_forward and enable_summary:
            quote = random.choice(self.output._apng_summary_quotes)
            sent = await self.output._send_forward_message(
                event, paths, summary=quote, profile="apng"
            )
            if sent:
                return
            sent = await self.output._send_image_with_summary(event, paths, profile="apng")
            if sent:
                return
        elif enable_forward:
            sent = await self.output._send_forward_message(event, paths, profile="apng")
            if sent:
                return
        elif enable_summary:
            sent = await self.output._send_image_with_summary(event, paths, profile="apng")
            if sent:
                return
        await event.send(
            event.chain_result(self.output._image_chain_with_at(event, paths, profile="apng"))
        )

    async def _handle_apng(self, event: AstrMessageEvent):
        try:
            duration_ms, loop, first_duration_ms = self._parse_apng_options(event.message_str)
        except ValueError as exc:
            return event.plain_result(f"❌ {exc}")
        refs = await self._extract_all_image_urls(event)
        max_frames = int(self.conf.get("apng_maker_max_frames", 20))
        min_frames = max(1, min(max_frames, int(self.conf.get("apng_maker_min_frames", 2))))
        if not min_frames <= len(refs) <= max_frames:
            return event.plain_result(f"❌ 请提供 {min_frames}～{max_frames} 张图片")
        if bool(self.conf.get("apng_enable_drawing_message", True)):
            message = str(self.conf.get("apng_drawing_message", "小猫正在搓屏幕中/ᐠ - ˕ -マ Ⳋ📱")).strip()
            if message:
                await event.send(event.plain_result(message))
        local_paths = []
        temporary_paths = []
        for ref in refs:
            original_is_local = Path(str(ref)).is_file()
            local = await self._materialize_message_image(event, ref)
            if not local:
                for path in temporary_paths:
                    Path(path).unlink(missing_ok=True)
                return event.plain_result("❌ 有图片下载失败，请重新发送后再试")
            local_paths.append(local)
            if not original_is_local and Path(local).parent.resolve() == self.save_dir.resolve():
                temporary_paths.append(local)
        durations = [duration_ms] * len(local_paths)
        if len(local_paths) == 1:
            first_frame = self.output._resolve_apng_first_frame()
            if first_frame is None:
                for path in temporary_paths:
                    Path(path).unlink(missing_ok=True)
                return event.plain_result("❌ 没有可用的默认首帧，请先用 /nc fs 设置")
            local_paths.insert(0, str(first_frame))
            durations.insert(0, first_duration_ms)
        elif durations:
            durations[0] = first_duration_ms
        output = ""
        retained_sources = []
        retained_thumbnails = []
        retain_history = not bool(self.conf.get("apng_cleanup_after_send", False)) and int(self.conf.get("apng_history_limit", 200)) > 0
        try:
            if retain_history:
                retained_sources = await asyncio.to_thread(self.output.save_history_sources, local_paths, "apng_source")
                retained_thumbnails = await asyncio.to_thread(self.output.save_history_thumbnails, retained_sources, "input")
            output = await asyncio.to_thread(
                self.output.make_apng, local_paths, duration_ms, loop, durations
            )
            output_size = Path(output).stat().st_size if Path(output).is_file() else 0
            await self._send_apng_file(event, output)
            if retain_history:
                try:
                    self.apng_history_store.add(
                        user_id=event.get_sender_id(),
                        group_id=event.get_group_id() or "",
                        frame_count=len(local_paths),
                        duration_ms=duration_ms,
                        loop=loop,
                        file_size=output_size,
                        source_image_paths=retained_sources,
                        source_thumbnail_paths=retained_thumbnails,
                    )
                    self.apng_history_store.enforce_limit(int(self.conf.get("apng_history_limit", 200)))
                    retained_sources = []
                    retained_thumbnails = []
                except Exception:  # noqa: BLE001
                    logger.warning("[neko_draw] APNG 记录写入失败", exc_info=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[neko_draw] APNG 制作失败", exc_info=True)
            return event.plain_result(f"❌ APNG 制作失败：{exc}")
        finally:
            if output:
                try:
                    Path(output).unlink(missing_ok=True)
                except OSError:
                    logger.warning("[neko_draw] APNG 成品清理失败: %s", output)
            self.output.cleanup_history_assets(retained_sources + retained_thumbnails)
            for path in temporary_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
        return None

    @filter.command("apng")
    async def apng_command(self, event: AstrMessageEvent):
        """将随消息发送的图片制作成 APNG。"""
        if not self.whitelist_guard.check(event.get_sender_id(), event.get_group_id()):
            return
        response = await self._handle_apng(event)
        if response is not None:
            yield response

    # ---------------- Neko Config 指令组 ----------------
    @filter.command_group("nc")
    def neko_config():
        """猫娘画图管理指令组。"""
        pass

    @neko_config.command("h")
    async def config_help_command(self, event: AstrMessageEvent):
        yield event.plain_result(
            "Neko Draw 指令\n"
            "绘图：nd <提示词>\n编辑：nde <提示词> + 图片\n"
            "动画：apng [间隔秒数] [循环次数] [--fd 首帧秒数] + 图片\n"
            "只输入 apng 时使用 WebUI 默认间隔（默认 5 秒）\n\n"
            "管理员 /nc 子指令\n"
            "pa/pd/pl/ps：添加、删除、列出、查看预设\n"
            "wa/wd/wl：添加、删除、列出白名单（u=用户，g=群组）\n"
            "fs/fv/fr：设置、查看、恢复 APNG 首帧\n"
            "ao/af：开启、关闭生成图片的 APNG 包装"
        )

    # ---------------- 预设提示词命令 ----------------
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("pa")
    async def add_prompt_command(self, event: AstrMessageEvent):
        """/nc pa <触发词> <提示词内容>"""
        rest = self._strip_command(event.message_str, ["nc pa"])
        if rest is None:
            rest = ""
        parts = rest.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("❌ 用法：/nc pa <触发词> <提示词内容>")
            return
        trigger, prompt_body = parts
        ok, msg = self.prompt_manager.add(trigger, prompt_body)
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("pd")
    async def del_prompt_command(self, event: AstrMessageEvent, trigger_word: str = ""):
        """/nc pd <触发词>"""
        trigger_word = trigger_word.strip()
        if not trigger_word:
            yield event.plain_result("❌ 用法：/nc pd <触发词>")
            return
        ok, msg = self.prompt_manager.delete(trigger_word)
        yield event.plain_result(f"{'🗑️' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("pl")
    async def list_prompts_command(self, event: AstrMessageEvent):
        """/nc pl"""
        triggers = self.prompt_manager.list_prompts()
        if not triggers:
            yield event.plain_result("📜 当前没有预设提示词。")
            return
        yield event.plain_result("📜 当前预设提示词列表：\n" + "、".join(triggers))

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("ps")
    async def prompt_details_command(
        self, event: AstrMessageEvent, trigger_word: str = ""
    ):
        """/nc ps <触发词>"""
        trigger_word = trigger_word.strip()
        if not trigger_word:
            yield event.plain_result("❌ 用法：/nc ps <触发词>")
            return
        detail = self.prompt_manager.get_detail(trigger_word)
        if detail is None:
            yield event.plain_result(f"❌ 未找到提示词：「{trigger_word}」")
            return
        yield event.plain_result(f"📋 提示词详情：「{trigger_word}」\n{detail}")

    # ---------------- 白名单命令 ----------------
    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("wa")
    async def add_whitelist_command(
        self, event: AstrMessageEvent, cmd_type: str = "", target_id: str = ""
    ):
        """/nc wa <u|g> <ID>"""
        cmd_type = {"u": "用户", "g": "群组"}.get(cmd_type.lower(), cmd_type)
        ok, msg = self.whitelist_guard.add(cmd_type, target_id)
        yield event.plain_result(f"{'✅' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("wd")
    async def del_whitelist_command(
        self, event: AstrMessageEvent, cmd_type: str = "", target_id: str = ""
    ):
        """/nc wd <u|g> <ID>"""
        cmd_type = {"u": "用户", "g": "群组"}.get(cmd_type.lower(), cmd_type)
        ok, msg = self.whitelist_guard.delete(cmd_type, target_id)
        yield event.plain_result(f"{'🗑️' if ok else '❌'} {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("wl")
    async def list_whitelist_command(self, event: AstrMessageEvent):
        """/nc wl"""
        yield event.plain_result(self.whitelist_guard.list_whitelist())

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("fs")
    async def set_first_frame_command(self, event: AstrMessageEvent):
        refs = self._extract_image_urls(event)
        if not refs:
            yield event.plain_result("❌ 请随 /nc fs 附带一张图片，或回复一张图片")
            return
        local = await self.downloader.materialize_image(refs[-1])
        if not local:
            yield event.plain_result("❌ 首帧图片下载失败")
            return
        try:
            target = await asyncio.to_thread(self.output.save_first_frame, local)
            self.conf["apng_first_frame_path"] = [str(target)]
            self._persist_config()
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"❌ 首帧保存失败：{exc}")
            return
        yield event.plain_result("✅ APNG 首帧已更新")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("fv")
    async def view_first_frame_command(self, event: AstrMessageEvent):
        path = self.output._resolve_apng_first_frame()
        if path is None:
            yield event.plain_result("❌ 当前没有可用的 APNG 首帧")
            return
        yield event.chain_result([Plain("当前 APNG 首帧："), Image.fromFileSystem(str(path))])

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("fr")
    async def reset_first_frame_command(self, event: AstrMessageEvent):
        self.conf["apng_first_frame_path"] = []
        self._persist_config()
        yield event.plain_result("✅ 已恢复插件内置 APNG 首帧")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("ao")
    async def enable_apng_command(self, event: AstrMessageEvent):
        self.conf["enable_apng_wrap"] = True
        self._persist_config()
        yield event.plain_result("✅ 生成图片的 APNG 首帧包装已开启")

    @filter.permission_type(filter.PermissionType.ADMIN, raise_error=False)
    @neko_config.command("af")
    async def disable_apng_command(self, event: AstrMessageEvent):
        self.conf["enable_apng_wrap"] = False
        self._persist_config()
        yield event.plain_result("✅ 生成图片的 APNG 首帧包装已关闭")

    # ---------------- 绘图消息入口 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_message(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator:
        """绘图命令消息入口。"""
        text = event.message_str.strip()
        if not text:
            return
        if not text.startswith("/") and self._strip_command(text, ["apng"]) is not None:
            if self.whitelist_guard.check(event.get_sender_id(), event.get_group_id()):
                response = await self._handle_apng(event)
                if response is not None:
                    yield response
            return
        if self._is_command_text(text):
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
        image_urls = await self._extract_all_image_urls(event)

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
            msg = str(self.conf.get("drawing_message", "小猫正在搓屏幕中/ᐠ - ˕ -マ Ⳋ📱")).strip()
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

        # 先把编辑原图落地：模型调用与历史预览使用同一份输入，避免 QQ
        # 临时图片链接在历史页面打开时已经失效。
        source_local_paths = []
        source_temporary_paths = []
        prepared_image_urls = []
        seen_source_images: set[str] = set()
        for ref in image_urls:
            original_is_local = Path(str(ref)).is_file()
            local = await self._materialize_message_image(event, ref)
            if local:
                digest = await asyncio.to_thread(image_content_digest, local)
                if digest and digest in seen_source_images:
                    # 两个解析入口偶尔会命中同一个缓存文件；只有当前重复项
                    # 是独立临时文件时才删除，不能误删首个仍要提交给模型的文件。
                    if (
                        local not in prepared_image_urls
                        and not original_is_local
                        and Path(local).parent.resolve() == self.save_dir.resolve()
                    ):
                        try:
                            Path(local).unlink(missing_ok=True)
                        except OSError:
                            pass
                    logger.debug("[IMAGE] 已跳过重复参考图: %s", str(ref)[:160])
                    continue
                if digest:
                    seen_source_images.add(digest)
                prepared_image_urls.append(local)
                source_local_paths.append(local)
                if not original_is_local and Path(local).parent.resolve() == self.save_dir.resolve():
                    source_temporary_paths.append(local)
            else:
                prepared_image_urls.append(ref)
        image_urls = prepared_image_urls

        _gen_start = time.time()
        result = None
        source_history_paths = []
        source_thumbnail_paths = []
        output_thumbnail_paths = []
        retain_history = not bool(self.conf.get("drawing_cleanup_after_send", False)) and int(self.conf.get("drawing_history_limit", 200)) > 0
        try:
            result = await self.handler.handle(
                text,
                image_urls,
                user_id=event.get_sender_id(),
                group_id=event.get_group_id(),
                progress_cb=_progress,
            )
            if retain_history and result and result.refer_image_count and source_local_paths:
                source_history_paths = await asyncio.to_thread(
                    self.output.save_history_sources, source_local_paths
                )
                source_thumbnail_paths = await asyncio.to_thread(self.output.save_history_thumbnails, source_history_paths, "input")
        finally:
            self.rate_limiter.finish(reservation, bool(result and result.images))
            for path in source_temporary_paths:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass
        _gen_ms = (time.time() - _gen_start) * 1000

        if result.silent:
            return

        # 记录生成历史（成功和失败都记，静默忽略的不记）
        history_saved = False
        if retain_history and result.model_template:
            try:
                if result.images:
                    output_thumbnail_paths = await asyncio.to_thread(self.output.save_history_thumbnails, result.images, "output")
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
                    source_image_paths=source_history_paths,
                    image_thumbnail_paths=output_thumbnail_paths,
                    source_thumbnail_paths=source_thumbnail_paths,
                    generation_time_ms=_gen_ms,
                )
                self.history_store.enforce_limit(int(self.conf.get("drawing_history_limit", 200)))
                history_saved = True
            except Exception:  # noqa: BLE001
                logger.warning("[neko_draw] 历史记录写入失败", exc_info=True)
                self.output.cleanup_history_assets(source_history_paths + source_thumbnail_paths + output_thumbnail_paths)

        if result.images:
            # 生成成功，记录限流使用（失败不扣次数）
            # 处理链第一步：APNG 动图包装（生成图 → APNG → 外显金句 → 合并转发）
            result_images = await asyncio.to_thread(self.output._wrap_apng, result.images)

            enable_forward = bool(self.conf.get("enable_forward_message", False))
            enable_summary = bool(self.conf.get("enable_image_summary", False))
            try:
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
            finally:
                # 生图 APNG 仅用于本次发送；历史记录始终指向原始生成图。
                await asyncio.to_thread(
                    self.output.cleanup_transient_apngs, result_images, result.images
                )
                if not history_saved:
                    await asyncio.to_thread(self.output.cleanup_generated_images, result.images)
        elif result.text:
            yield event.plain_result(result.text)

    async def terminate(self):
        """卸载时关闭所有网络会话。"""
        await self._runtime.close()
