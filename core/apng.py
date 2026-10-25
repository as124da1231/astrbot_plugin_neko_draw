# -*- coding: utf-8 -*-
"""APNG 制作：多张图片 → 动图，并保留输入原图历史。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

from .config_store import get, get_int
from .image_collector import SOURCE_MESSAGE, ImageCollector
from .image_dedupe import image_content_digest
from .image_sender import PROFILE_APNG, ImageSender
from .objects import remove_files

USAGE = (
    "用法：apng [间隔秒数] [循环次数] [--fd 首帧秒数]\n"
    "间隔 0.1～60 秒；循环 0～100（0 表示无限循环）；首帧 0.02～60 秒。\n"
    "例如：apng 4        （每帧 4 秒）\n"
    "      apng 4 3      （每帧 4 秒，播放 3 次）\n"
    "      apng 4 --fd 2 （每帧 4 秒，首帧停留 2 秒）"
)

# 单张图片的落地上限（秒）：动图一次要下多张，不能让某一张无限等
IMPORT_TIMEOUT_SECONDS = 100

# 参数解析失败的几种具体原因（比单纯回一句“格式错误”更能指导用户）
REASON_MISSING_FD_VALUE = "--fd 后面要跟一个秒数，例如 apng 4 --fd 2"
REASON_EXTRA = "参数太多了，只支持：间隔秒数、循环次数、--fd 首帧秒数"


def _parse_number(value: str, kind: str, cast):
    """把一个参数解析成数字；失败时给出“哪一项错了”，而不是笼统的用法。"""
    try:
        return cast(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{kind}「{value}」不是有效数字") from exc


class ApngService:
    """把用户发来的多张图片做成 APNG 动图。"""

    def __init__(
        self,
        *,
        conf: Any,
        collector: ImageCollector,
        output: Any,
        sender: ImageSender,
        history: Any,
        overrides: Optional[dict] = None,
    ):
        self.conf = conf
        self.collector = collector
        self.output = output
        self.sender = sender
        self.history = history
        # 与 App 共享同一份字典：插件实例上的替换会立刻生效
        self.overrides: dict = overrides if overrides is not None else {}

    # ------------------------------------------------------------------ 参数
    def parse_options(self, text: str) -> tuple[int, int, int]:
        """解析 ``apng [间隔秒数] [循环次数] [--fd 首帧秒数]``，返回毫秒。

        失败时抛 ``ValueError``，文案直接能照做：位置错、数字错、越界都会说清楚。
        """
        from .text_utils import split_command

        rest = split_command(text, ["apng"]) or ""
        parts = rest.split()

        try:
            seconds = float(get(self.conf, "apng_default_interval_seconds", 5))
            loop = get_int(self.conf, "apng_loop", 0)
            first_seconds = get_int(self.conf, "apng_first_frame_duration", 100) / 1000
        except (TypeError, ValueError):
            # 配置本身写坏了：用安全默认值，不把用户卡在“什么都没发生”
            seconds, loop, first_seconds = 5.0, 0, 0.1

        positionals: list[str] = []
        fd_index = -1
        for index, token in enumerate(parts):
            if token in {"--fd", "-fd"}:
                fd_index = index
                break
            positionals.append(token)

        if len(positionals) > 2:
            raise ValueError(REASON_EXTRA)
        if positionals:
            seconds = _parse_number(positionals[0], "每帧间隔", float)
        if len(positionals) == 2:
            loop = _parse_number(positionals[1], "循环次数", int)

        if fd_index >= 0:
            if fd_index + 1 >= len(parts):
                raise ValueError(REASON_MISSING_FD_VALUE)
            first_seconds = _parse_number(parts[fd_index + 1], "--fd 首帧时长", float)
            if len(parts) > fd_index + 2:
                raise ValueError(REASON_EXTRA)

        if not 0.1 <= seconds <= 60:
            raise ValueError(f"每帧间隔必须为 0.1～60 秒，现在是 {seconds:g} 秒")
        if not 0 <= loop <= 100:
            raise ValueError(f"循环次数必须为 0～100（0 表示无限循环），现在是 {loop}")
        if not 0.02 <= first_seconds <= 60:
            raise ValueError(f"首帧停留时间必须为 0.02～60 秒，现在是 {first_seconds:g} 秒")
        # 下游 make_apng 按毫秒校验（100～60000ms / 20～60000ms），而这里算的是“秒”。
        # 两者口径必须一致：0.0999 秒能过秒级判断，却会被毫秒级判断拒绝。
        duration_ms = round(seconds * 1000)
        first_ms = round(first_seconds * 1000)
        if not 100 <= duration_ms <= 60000:
            raise ValueError(f"每帧间隔必须为 0.1～60 秒，现在是 {seconds:g} 秒")
        if not 20 <= first_ms <= 60000:
            raise ValueError(f"首帧停留时间必须为 0.02～60 秒，现在是 {first_seconds:g} 秒")
        return duration_ms, loop, first_ms

    async def run_as_result(self, event: Any):
        """执行并返回可直接 yield 的回复对象（无错误时返回 None）。"""
        message = await self.run(event)
        return None if message is None else event.plain_result(message)

    # ------------------------------------------------------------------ 提示
    @staticmethod
    def _frames_hint(count: int, min_frames: int, max_frames: int) -> str:
        """图片张数不合适时，直接说清楚“收到几张、需要几张、还差几张”。"""
        if count == 0:
            return (
                f"❌ 没有取到图片：{min_frames}～{max_frames} 张都能做，"
                "可以直接发送图片、回复一条带图的消息，或发一条合并转发的消息"
            )
        if count < min_frames:
            return (
                f"❌ 只收到 {count} 张图片，最少要 {min_frames} 张，"
                f"再补 {min_frames - count} 张就能开始制作"
            )
        return f"❌ 收到 {count} 张图片，最多 {max_frames} 张，请分批制作"

    # ------------------------------------------------------------------ 主流程
    async def run(self, event: Any) -> Optional[str]:
        """执行一次 APNG 制作，返回需要回复的错误文本；成功返回 None。"""
        try:
            duration_ms, loop, first_duration_ms = self.parse_options(getattr(event, "message_str", ""))
        except ValueError as exc:
            return f"❌ {exc}"

        # 引用 → 来源。APNG 去重要知道“哪张是用户明确发的”，不能只看文件名：
        # 自动命名的 remote.jpg 在补充解析器和消息链里可能撞名。
        refs_by_source = await self.collector.collect_all_by_source(
            event, overrides=ImageCollector.overrides_from(self.overrides)
        )
        refs = list(refs_by_source)
        # 平台 / 配置里可能填成字符串甚至脏值；解析失败就按默认区间走，
        # 不要把“张数配置写错”变成整条命令崩掉。
        max_frames = max(1, get_int(self.conf, "apng_maker_max_frames", 20))
        min_frames = get_int(self.conf, "apng_maker_min_frames", 2)
        min_frames = max(1, min(max_frames, min_frames))
        if not refs or len(refs) > max_frames:
            return self._frames_hint(len(refs), min_frames, max_frames)

        # 落地：走统一的 materialize_many（同一份去重逻辑，覆盖项也统一）
        # 单张图限时 100 秒：动图一次要下很多张，不能某一张卡住就把整条请求拖死
        landed, landed_temporary, failed_refs = await self.collector.materialize_many(
            event,
            refs,
            ImageCollector.overrides_from(self.overrides),
            timeout=IMPORT_TIMEOUT_SECONDS,
        )
        if failed_refs:
            # 有图真的没下载下来：撤掉已落的临时文件，并说清缺了几张
            remove_files(landed_temporary)
            return (
                f"❌ 有 {len(failed_refs)} 张图片没能取到（超时或下载失败），"
                "请重发这几张再试"
            )

        # landed 已经是“保序去重后”的本地路径。这里再做一次“来源感知”的去重：
        # 同一张图被用户自己发了两遍（都是本条消息）时保留两次，来自补充解析器的副本丢掉。
        written_paths: set[str] = set()
        local_paths: list[str] = []
        temporary_paths: list[str] = []
        seen_digests: dict[str, bool] = {}   # 内容指纹 → 首次出现是否为“明确发送”

        for ref, local in zip(refs, landed):
            if local is None:
                # 被前面同内容的图“去重”掉了（不算失败，失败已在上面单独处理）
                logger.debug("[APNG] 已跳过重复图片: %s", str(ref)[:160])
                continue
            # “用户明确发的”按来源判断，而不是按引用字符串是否出现在消息链里
            is_primary = refs_by_source.get(str(ref)) == SOURCE_MESSAGE
            digest = await asyncio.to_thread(image_content_digest, local)

            if digest and digest in seen_digests:
                # 同一张图重复出现：
                # - 用户明确发了两次（都是 primary）→ 保留，视为有意重复
                # - 有一份来自补充解析器 → 丢掉后来这份（也可能是同一张本地图，不能删）
                if not (is_primary and seen_digests[digest]):
                    if local in landed_temporary and local not in written_paths:
                        remove_files([local])
                    logger.debug("[APNG] 已跳过重复图片: %s", str(ref)[:160])
                    continue

            if digest:
                seen_digests.setdefault(digest, is_primary)
            local_paths.append(local)
            written_paths.add(local)
            if local in landed_temporary:
                temporary_paths.append(local)

        if not min_frames <= len(local_paths) <= max_frames:
            remove_files(temporary_paths)
            return self._frames_hint(len(local_paths), min_frames, max_frames)

        if bool(self.conf.get("apng_enable_drawing_message", True)):
            message = str(
                self.conf.get("apng_drawing_message", "小猫正在搓屏幕中/ᐠ - ˕ -マ Ⳋ📱")
            ).strip()
            if message:
                try:
                    await event.send(event.plain_result(message))
                except Exception:  # noqa: BLE001
                    logger.debug("[APNG] 开始提示发送失败", exc_info=True)

        # 历史中的“输入原图”只表示用户本次真正提交的图片
        history_input_paths = list(local_paths)
        durations = [duration_ms] * len(local_paths)
        if len(local_paths) == 1:
            first_frame = self.output._resolve_apng_first_frame()
            if first_frame is None:
                remove_files(temporary_paths)
                return "❌ 没有可用的默认首帧，请先用 /nc fs 设置"
            local_paths.insert(0, str(first_frame))
            durations.insert(0, first_duration_ms)
        elif durations:
            durations[0] = first_duration_ms

        output = ""
        delivered = True
        retained_sources: list[str] = []
        retained_thumbnails: list[str] = []
        retain_history = (
            not bool(self.conf.get("apng_cleanup_after_send", False))
            and get_int(self.conf, "apng_history_limit", 200) > 0
        )
        try:
            if retain_history:
                retained_sources = await asyncio.to_thread(
                    self.output.save_history_sources, history_input_paths, "apng_source"
                )
                retained_thumbnails = await asyncio.to_thread(
                    self.output.save_history_thumbnails, retained_sources, "input"
                )
            output = await asyncio.to_thread(
                self.output.make_apng, local_paths, duration_ms, loop, durations
            )
            output_size = Path(output).stat().st_size if Path(output).is_file() else 0
            # send_images 成功时返回 None，整条发送失败时返回 False
            delivered = (
                await self.sender.send_images(event, [output], profile=PROFILE_APNG)
            ) is not False
            # 发送失败时不写“成功”历史：那条记录点开是空的，比没有更让人困惑。
            # 这里只是记一句、不 return，保证 finally 里的临时文件/历史素材照常清理。
            if not delivered:
                logger.warning("[neko_draw] APNG 已生成但发送失败，本次不写历史")
            elif retain_history:
                try:
                    self.history.add(
                        user_id=event.get_sender_id(),
                        group_id=event.get_group_id() or "",
                        frame_count=len(local_paths),
                        duration_ms=duration_ms,
                        loop=loop,
                        file_size=output_size,
                        source_image_paths=retained_sources,
                        source_thumbnail_paths=retained_thumbnails,
                    )
                    self.history.enforce_limit(get_int(self.conf, "apng_history_limit", 200))
                    retained_sources = []
                    retained_thumbnails = []
                except Exception:  # noqa: BLE001
                    logger.warning("[neko_draw] APNG 记录写入失败", exc_info=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[neko_draw] APNG 制作失败", exc_info=True)
            return f"❌ APNG 制作失败：{exc}"
        finally:
            if output:
                remove_files([output])
            # 只有真的写进历史时才需要保留输入素材；发送失败 / 制作失败都要清掉
            if retained_sources or retained_thumbnails:
                await asyncio.to_thread(
                    self.output.cleanup_history_assets,
                    retained_sources + retained_thumbnails,
                )
            remove_files(temporary_paths)
        # send_images 的约定是“失败返回 False，成功返回 None”，所以只能和 False 比
        if delivered is False:
            return "❌ 动图做好了但没能发送出去，本次不记入历史；请检查图片发送设置后重试"
        return None
