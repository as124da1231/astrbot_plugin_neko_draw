"""Image transformations and platform delivery; original ordering and fallbacks."""
import json
import random
import time
import uuid
from pathlib import Path
from typing import Optional
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image
try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

class OutputService:
    def __init__(self, config, data_dir, save_dir, plugin_dir):
        self.conf = config
        self.data_dir = data_dir
        self.save_dir = save_dir
        self.plugin_dir = plugin_dir
        self._image_summary_quotes = self._load_summary_quotes()
        self._apng_summary_quotes = self._load_summary_quotes("apng_")

    def _load_summary_quotes(self, prefix: str = "") -> list[str]:
        """加载图片外显金句：配置列表 + 金句文件，合并去重。"""
        quotes_key = f"{prefix}image_summary_quotes"
        files_key = f"{prefix}image_summary_quotes_files"
        quotes: list[str] = list(self.conf.get(quotes_key, []) or [])
        for file_path in self.conf.get(files_key, []) or []:
            path = Path(file_path)
            if not path.exists():
                logger.warning(f"[neko_draw] 金句文件不存在，已跳过：{path}")
                continue
            try:
                with path.open(encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        quotes.extend(str(q) for q in data if str(q).strip())
                    else:
                        logger.warning(f"[neko_draw] 金句文件内容不是数组，已跳过：{path}")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"[neko_draw] 读取金句文件失败 {path}: {e}")
        # 去重并过滤空字符串
        seen = set()
        result = []
        for q in quotes:
            q = q.strip()
            if q and q not in seen:
                seen.add(q)
                result.append(q)
        return result or ["[图片]"]

    def _resolve_file_ref(self, raw) -> Optional[Path]:
        """将 file 上传类型的配置值解析为绝对路径。

        支持 list（取最后一张有效）和 str（直接用）。
        相对路径尝试：插件数据目录、插件数据目录/files、插件目录、当前工作目录。
        绝对路径直接检查。返回 None 表示未找到。
        """
        if isinstance(raw, list):
            paths = [str(p).strip() for p in reversed(raw) if str(p).strip()]
        elif isinstance(raw, str) and raw.strip():
            paths = [raw.strip()]
        else:
            return None

        for candidate in paths:
            p = Path(candidate)
            if p.is_absolute() and p.is_file():
                return p
            clean = candidate.lstrip("/\\")
            for base in (self.data_dir, self.data_dir / "files", self.plugin_dir, Path.cwd()):
                candidate_path = base / clean
                if candidate_path.is_file():
                    return candidate_path
        return None

    def _resolve_apng_first_frame(self, profile: str = "apng") -> Optional[Path]:
        """解析 APNG 第一帧图片路径，支持 file 上传、手动路径和内置默认图。

        优先级：用户上传的 file（取最后一张）> 手动配置的路径 > 内置默认图。
        """
        key = "drawing_first_frame_path" if profile == "drawing" else "apng_first_frame_path"
        raw = self.conf.get(key, "")
        resolved = self._resolve_file_ref(raw)
        if resolved is not None:
            return resolved
        # 内置默认图作为最后回退
        default_frame = self.plugin_dir / "default_apng_frame.png"
        if default_frame.exists():
            return default_frame
        return None

    def _wrap_apng(self, image_paths: list[str]) -> list[str]:
        """将生成的图片包装成两帧 APNG 动图。

        第一帧：配置的首帧图片（等比缩放+居中+白底填充到生成图尺寸）
        第二帧：生成的图片
        返回 APNG 文件路径列表。若未开启、Pillow 不可用、首帧路径无效，
        则直接返回原图路径列表。
        """
        if not bool(self.conf.get("enable_apng_wrap", False)):
            return image_paths
        if PILImage is None:
            logger.warning("[neko_draw] Pillow 未安装，APNG 功能已禁用")
            return image_paths

        first_file = self._resolve_apng_first_frame("drawing")
        if first_file is None:
            logger.warning("[neko_draw] 未找到 APNG 第一帧图片（含内置默认图），APNG 功能已禁用")
            return image_paths

        first_duration = int(self.conf.get("drawing_first_frame_duration", 100))
        second_duration = int(self.conf.get("drawing_second_frame_duration", 20000))
        loop = int(self.conf.get("drawing_apng_loop", 0))
        optimize = bool(self.conf.get("drawing_apng_optimize", True))

        result_paths = []
        try:
            first_src = PILImage.open(first_file).convert("RGBA")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[neko_draw] 打开 APNG 首帧失败: {e}")
            return image_paths

        for idx, img_path in enumerate(image_paths):
            try:
                # 第二帧：生成的图片
                second = PILImage.open(img_path).convert("RGBA")
                target_w, target_h = second.size

                # 第一帧：等比缩放 + 居中 + 白底填充
                first = first_src.copy()
                try:
                    resample = PILImage.Resampling.LANCZOS
                except AttributeError:  # Pillow < 9.1 兼容
                    resample = PILImage.LANCZOS
                first.thumbnail((target_w, target_h), resample)
                canvas = PILImage.new("RGBA", (target_w, target_h), (255, 255, 255, 255))
                offset = ((target_w - first.width) // 2, (target_h - first.height) // 2)
                canvas.paste(first, offset, first)
                first = canvas

                # 保存 APNG
                out_path = self.save_dir / f"apng_{uuid.uuid4().hex}_{idx}.png"
                first.save(
                    out_path,
                    format="PNG",
                    save_all=True,
                    append_images=[second],
                    duration=[first_duration, second_duration],
                    loop=loop,
                    optimize=optimize,
                    disposal=2,
                )
                result_paths.append(str(out_path))
                logger.info(
                    f"[neko_draw] APNG 合成完成: {out_path.name} "
                    f"({target_w}x{target_h}, 首帧{first_duration}ms/次帧{second_duration}ms)"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[neko_draw] APNG 合成失败，使用原图: {e}")
                result_paths.append(img_path)

        return result_paths

    def cleanup_transient_apngs(
        self, delivery_paths: list[str], original_paths: list[str]
    ) -> None:
        """Delete generated-image APNG wrappers while keeping original images.

        The strict directory and filename checks exclude APNG-command works,
        which remain governed by their independent history/cleanup setting.
        """
        originals = {str(Path(path).resolve()) for path in original_paths}
        save_root = self.save_dir.resolve()
        for raw_path in delivery_paths:
            path = Path(raw_path)
            try:
                resolved = path.resolve()
                if str(resolved) in originals or resolved.parent != save_root:
                    continue
                if not resolved.name.startswith("apng_") or resolved.suffix.lower() != ".png":
                    continue
                resolved.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"[neko_draw] 临时生图 APNG 清理失败 {path}: {exc}")

    def make_apng(
        self,
        image_paths: list[str],
        duration_ms: int,
        loop: int = 0,
        durations: Optional[list[int]] = None,
    ) -> str:
        """将多张图片按消息顺序合成为 APNG，并返回生成文件路径。"""
        if PILImage is None:
            raise RuntimeError("Pillow 未安装，无法制作 APNG")
        max_frames = int(self.conf.get("apng_maker_max_frames", 20))
        max_dimension = int(self.conf.get("apng_maker_max_dimension", 2048))
        if not 2 <= len(image_paths) <= max_frames:
            raise ValueError(f"图片数量必须为 2～{max_frames} 张")
        if not 100 <= int(duration_ms) <= 60000:
            raise ValueError("每帧间隔必须为 0.1～60 秒")
        if not 0 <= int(loop) <= 100:
            raise ValueError("循环次数必须为 0～100，0 表示无限循环")
        frame_durations = durations or [int(duration_ms)] * len(image_paths)
        if len(frame_durations) != len(image_paths):
            raise ValueError("帧时长数量与图片数量不一致")

        opened = []
        try:
            for path in image_paths:
                with PILImage.open(path) as source:
                    source.seek(0)
                    opened.append(source.convert("RGBA"))
            width = min(max(image.width for image in opened), max_dimension)
            height = min(max(image.height for image in opened), max_dimension)
            try:
                resample = PILImage.Resampling.LANCZOS
            except AttributeError:
                resample = PILImage.LANCZOS

            frames = []
            for image in opened:
                image.thumbnail((width, height), resample)
                canvas = PILImage.new("RGBA", (width, height), (255, 255, 255, 255))
                offset = ((width - image.width) // 2, (height - image.height) // 2)
                canvas.paste(image, offset, image)
                frames.append(canvas)

            out_path = self.save_dir / f"apng_maker_{uuid.uuid4().hex}.png"
            frames[0].save(
                out_path,
                format="PNG",
                save_all=True,
                append_images=frames[1:],
                duration=[max(20, int(value)) for value in frame_durations],
                loop=int(loop),
                optimize=bool(self.conf.get("apng_optimize", True)),
                disposal=2,
            )
            return str(out_path)
        finally:
            for image in opened:
                image.close()

    def save_first_frame(self, source_path: str) -> Path:
        """验证图片并保存为 QQ 指令设置的持久首帧。"""
        files_dir = self.data_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        target = files_dir / "apng_first_frame_qq.png"
        with PILImage.open(source_path) as source:
            source.seek(0)
            source.convert("RGBA").save(target, format="PNG")
        return target

    def save_history_sources(self, source_paths: list[str], prefix: str = "source") -> list[str]:
        """复制插件收到的原图，作为可独立清理的历史资产。"""
        target_dir = self.data_dir / "history_inputs"
        target_dir.mkdir(parents=True, exist_ok=True)
        saved = []
        for index, source_path in enumerate(source_paths):
            try:
                safe_prefix = "apng_source" if prefix == "apng_source" else "source"
                target = target_dir / f"{safe_prefix}_{uuid.uuid4().hex}_{index}.png"
                with PILImage.open(source_path) as source:
                    source.seek(0)
                    source.convert("RGBA").save(target, format="PNG")
                saved.append(str(target))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[neko_draw] 编辑原图保存失败，已跳过: {exc}")
        return saved

    def save_history_thumbnails(self, image_paths: list[str], prefix: str) -> list[str]:
        """为历史列表生成轻量缩略图；失败项以空字符串占位以保持索引。"""
        target_dir = self.data_dir / "history_thumbnails"
        target_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        safe_prefix = "input" if prefix == "input" else "output"
        for index, image_path in enumerate(image_paths):
            target = target_dir / f"thumb_{safe_prefix}_{uuid.uuid4().hex}_{index}.webp"
            try:
                with PILImage.open(image_path) as source:
                    source.seek(0)
                    image = source.convert("RGB")
                    image.thumbnail((360, 360))
                    image.save(target, format="WEBP", quality=72, method=4)
                saved.append(str(target))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[neko_draw] 历史缩略图生成失败，已跳过: {exc}")
                saved.append("")
        return saved

    def cleanup_generated_images(self, image_paths: list[str]) -> None:
        """删除本插件生成且尚未写入历史的原始结果。"""
        save_root = self.save_dir.resolve()
        for value in image_paths:
            try:
                path = Path(value).resolve()
                if path.parent == save_root and path.name.startswith("img_"):
                    path.unlink(missing_ok=True)
            except (OSError, ValueError) as exc:
                logger.warning(f"[neko_draw] 生图结果清理失败 {value}: {exc}")

    def cleanup_history_assets(self, paths: list[str]) -> None:
        """清理尚未绑定历史记录的输入副本或缩略图。"""
        roots = {
            (self.data_dir / "history_inputs").resolve(): ("source_", "apng_source_"),
            (self.data_dir / "history_thumbnails").resolve(): ("thumb_",),
        }
        for value in paths:
            if not value:
                continue
            try:
                path = Path(value).resolve()
                prefixes = roots.get(path.parent)
                if prefixes and path.name.startswith(prefixes):
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

    @staticmethod
    def _profile_key(profile: str, key: str) -> str:
        return f"apng_{key}" if profile == "apng" else key

    def _image_chain_with_at(
        self, event: AstrMessageEvent, image_paths: list[str], profile: str = "drawing"
    ) -> list:
        """构建带 @ 触发者的图片消息链（用于普通图片发送）。

        若开启了 enable_at_sender 且是群聊，则在图片前插入 At 组件；
        私聊或未开启时直接返回图片列表。
        """
        components = []
        at_key = self._profile_key(profile, "enable_at_sender")
        if bool(self.conf.get(at_key, False)) and event.get_group_id():
            components.append(At(event.get_sender_id()))
        components.extend([Image.fromFileSystem(p) for p in image_paths])
        return components

    async def _send_image_with_summary(
        self, event: AstrMessageEvent, image_paths: list[str], profile: str = "drawing"
    ) -> bool:
        """发送带外显金句的图片消息。

        原理：把 Image 组件转成 OneBot JSON，在图片消息段的 data 中
        设置 summary 字段为随机金句，然后直接用 bot.send() 发送原始
        OneBot 消息。这样 QQ 群列表中原本显示「[图片]」的位置会显示
        金句文字，点开后仍是正常图片。仅 aiocqhttp 平台且单张图片时生效。
        """
        # 图片外显仅支持单张图片
        if len(image_paths) != 1:
            return False

        # 延迟导入，避免非 aiocqhttp 环境报错
        try:
            from astrbot.core.message.message_event_result import MessageChain
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except ImportError:
            return False

        if not isinstance(event, AiocqhttpMessageEvent):
            return False

        try:
            # 把 Image 组件转成 OneBot JSON 消息段
            chain = MessageChain([Image.fromFileSystem(image_paths[0])])
            obmsg = await event._parse_onebot_json(chain)
            # 设置图片外显金句（核心）
            quotes = self._apng_summary_quotes if profile == "apng" else self._image_summary_quotes
            quote = random.choice(quotes)
            obmsg[0]["data"]["summary"] = quote
            # 若开启了 @ 触发者且是群聊，在消息前插入 at 消息段
            at_key = self._profile_key(profile, "enable_at_sender")
            if bool(self.conf.get(at_key, False)) and event.get_group_id():
                obmsg.insert(0, {"type": "at", "data": {"qq": str(event.get_sender_id())}})
            # 直接发送原始 OneBot 消息
            await event.bot.send(event.message_obj.raw_message, obmsg)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[neko_draw] 图片外显发送失败，回退普通图片: {e}"
            )
            return False

    async def _send_forward_message(
        self, event: AstrMessageEvent, image_paths: list[str], summary: str = "",
        profile: str = "drawing",
    ) -> bool:
        """直接调用 OneBot v11 原始 API 发送合并转发消息。

        把图片打包成 QQ 合并转发（聊天记录）卡片发出。
        若传入 summary，则合并转发节点内的图片消息段会带上 summary
        字段（图片外显金句），实现外显+合并转发同时生效。
        仅 aiocqhttp 平台支持，其他平台返回 False 由调用方回退。
        """
        try:
            platform = event.get_platform_name()
        except Exception:  # noqa: BLE001
            platform = ""
        if platform != "aiocqhttp":
            return False

        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        bot_id = event.get_self_id()

        # 构建节点内容：图片（可选带 summary 外显金句）
        content = []
        # 若开启了 @ 触发者且是群聊，在节点内容最前面插入 at 消息段
        at_key = self._profile_key(profile, "enable_at_sender")
        if bool(self.conf.get(at_key, False)) and group_id:
            content.append({"type": "at", "data": {"qq": str(user_id)}})
        for p in image_paths:
            img_data = {"file": f"file://{p}"}
            if summary:
                img_data["summary"] = summary
            content.append({"type": "image", "data": img_data})

        nickname_key = self._profile_key(profile, "forward_node_name")
        nickname = str(self.conf.get(nickname_key, "")).strip()
        if not nickname:
            nickname = "猫娘画图"

        forward_msg = [
            {
                "type": "node",
                "data": {
                    "user_id": int(bot_id) if bot_id else 10000,
                    "nickname": nickname,
                    "content": content,
                },
            }
        ]

        try:
            bot_api = getattr(event.bot, "api", None)
            if bot_api is None:
                logger.warning("[neko_draw] event.bot.api 不存在，无法发送合并转发")
                return False
            if group_id:
                await bot_api.call_action(
                    "send_group_forward_msg",
                    group_id=int(group_id),
                    messages=forward_msg,
                )
            else:
                await bot_api.call_action(
                    "send_private_forward_msg",
                    user_id=int(user_id),
                    messages=forward_msg,
                )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[neko_draw] 合并转发发送失败: {e}"
            )
            return False
