# -*- coding: utf-8 -*-
"""Neko Draw 新版入口装配。

``main.py`` 只保留 AstrBot 需要的 ``Star`` 子类与指令声明；真正的服务
全部在这里组装，方便替换测试替身。

依赖方向（单向，无环）：
    main → app → flow → {generation, text_tools, apng, pending_delivery}
                     ↘ {collector, sender, objects}
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .apng import ApngService
from .config_store import get_int
from .flow import FlowDeps, MessageFlow
from .history import HistoryStore
from .apng_history import ApngHistoryStore
from .image_collector import ImageCollector
from .image_sender import ImageSender
from .llm_gateway import LlmGateway
from .pending_delivery import PendingDelivery
from .runtime import RuntimeServices
from .text_tools import TextToolService

# 旧版本内置文案 → 新文案（只在用户仍用旧默认值时迁移）
DEFAULT_DRAWING_MESSAGE = "小猫正在搓屏幕中/ᐠ - ˕ -マ Ⳋ📱"
LEGACY_DRAWING_MESSAGES = ("🐱 猫娘正在画图，请稍候...", "小猫正在搓屏幕中")
LEGACY_APNG_MESSAGES = ("🐱 猫娘正在制作 APNG，请稍候...",)


@dataclass
class AppServices:
    """一次装配出的全部运行期服务。"""

    runtime: RuntimeServices
    history: HistoryStore
    apng_history: ApngHistoryStore
    collector: ImageCollector
    gateway: LlmGateway
    sender: ImageSender
    generation: Any  # core.handler.DrawingHandler
    text_tools: TextToolService
    apng: ApngService
    pending: PendingDelivery
    flow: MessageFlow


def migrate_legacy_messages(config) -> None:
    """仅迁移旧版内置默认文案；用户填写的其他自定义内容保持不变。"""
    try:
        if config.get("drawing_message") in set(LEGACY_DRAWING_MESSAGES):
            config["drawing_message"] = DEFAULT_DRAWING_MESSAGE
        if config.get("apng_drawing_message") in set(LEGACY_APNG_MESSAGES):
            config["apng_drawing_message"] = DEFAULT_DRAWING_MESSAGE
    except Exception:  # noqa: BLE001
        logger.debug("[neko_draw] 旧文案迁移失败", exc_info=True)


# 一次性执行的迁移标记：记录「生图预处理已强制关闭」，之后不再覆盖用户设置
PREPROCESS_RESET_MARKER = "preprocess_reset.json"
PREPROCESS_RESET_VERSION = 1


def reset_preprocess_once(config, data_dir) -> None:
    """升级时强制关闭所有人的生图预处理（只执行一次）。

    3.9.2 起「拆解 tag」被删除，生图预处理改由「文字加工 → 扩写」承担，
    默认关闭。老用户原本开着拆解的，这里连同每条预设的「预处理」三态与
    自定义拆解模板一起清掉，保证升级后行为统一为「不预处理」。

    只执行一次：标记文件存在后，用户在界面上重新打开的设置不会再被覆盖。
    """
    marker = Path(data_dir) / PREPROCESS_RESET_MARKER
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
        if int((state or {}).get("version", 0) or 0) >= PREPROCESS_RESET_VERSION:
            return
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("[neko_draw] 读取预处理迁移标记失败，按未迁移处理", exc_info=True)

    saver_ok = False
    try:
        config["llm_tag_enable"] = False
        presets = config.get("prompt")
        if isinstance(presets, list):
            for item in presets:
                if isinstance(item, dict):
                    item["decompose"] = ""
                    item["tag_prompt"] = ""
        saver = getattr(config, "save_config", None)
        if callable(saver):
            saver()
        saver_ok = True
    except Exception:  # noqa: BLE001
        logger.warning("[neko_draw] 关闭生图预处理失败，请在配置里手动关闭", exc_info=True)

    if not saver_ok:
        # 没写进磁盘就落下标记，会导致以后永远不再尝试迁移，
        # 用户升级后“预处理又自己打开了”却再也不会被修正。
        logger.warning("[neko_draw] 预处理迁移未写入磁盘，本次不记录迁移标记")
        return

    try:
        marker.write_text(
            json.dumps({"version": PREPROCESS_RESET_VERSION}), encoding="utf-8"
        )
    except OSError:
        logger.debug("[neko_draw] 写入预处理迁移标记失败", exc_info=True)


class App:
    """把运行期服务与消息流程粘在一起，并支持配置重载。"""

    def __init__(
        self,
        *,
        context,
        config,
        data_dir: Path,
        save_dir: Path,
        plugin_dir: Path,
        history: HistoryStore,
        apng_history: ApngHistoryStore,
    ):
        self.context = context
        self.conf = config
        self.data_dir = Path(data_dir)
        self.save_dir = Path(save_dir)
        self.plugin_dir = Path(plugin_dir)
        self._history = history
        self.apng_history = apng_history
        self.runtime: RuntimeServices = RuntimeServices.build(
            config,
            context=context,
            data_dir=self.data_dir,
            save_dir=self.save_dir,
            plugin_dir=self.plugin_dir,
        )
        # 旧方法名的覆盖项（plugin.__setattr__ 写入），主流程调用前会先查这里
        self.overrides: dict = {}
        # 在途消息计数：重载时若还有人正在生成，旧的网络连接必须等它跑完再关，
        # 否则“画到一半点保存”会直接把这次生成打断（连接被关闭 → 报错）。
        self._active_requests = 0
        self._retired: list = []
        self._build_services()

    # ------------------------------------------------------------------ 装配
    def _build_services(self) -> None:
        rt = self.runtime
        self.collector = ImageCollector(
            downloader=rt.downloader, save_dir=self.save_dir, conf=self.conf
        )
        self.gateway = LlmGateway(context=self.context, conf=self.conf)
        self.sender = ImageSender(conf=self.conf, output=rt.output)
        # 生图实现只有一份：runtime.handler（core/handler.py）。
        # flow 只负责“拿结果 → 写历史 → 发图”的编排。
        self.generation = rt.handler
        self.text_tools = TextToolService(
            conf=self.conf,
            gateway=self.gateway,
            collector=self.collector,
            store_getter=self.command_stores,
            history_recorder=self.record_llm_call,
            overrides=self.overrides,
        )
        self.text_tools.prompt_manager = rt.prompt_manager
        self.apng = ApngService(
            conf=self.conf,
            collector=self.collector,
            output=rt.output,
            sender=self.sender,
            history=self.apng_history,
            overrides=self.overrides,
        )
        self.pending = PendingDelivery(
            conf=self.conf,
            history=self.history,
            history_getter=self._history_getter,
            output=rt.output,
            downloader=rt.downloader,
            sender=self.sender,
            providers_getter=lambda: self.runtime.providers,
            context=self.context,
        )
        self.flow = MessageFlow(FlowDeps(
            conf=self.conf,
            prompt_manager=rt.prompt_manager,
            whitelist=rt.whitelist_guard,
            rate_limiter=rt.rate_limiter,
            generation=self.generation,
            collector=self.collector,
            sender=self.sender,
            apng=self.apng,
            text_tools=self.text_tools,
            pending=self.pending,
            output=rt.output,
            history=self.history,
            history_getter=self._history_getter,
            llm_history=rt.llm_history,
            expand_store_getter=self.command_stores,
            overrides=self.overrides,
            runtime=rt,
        ))

    @property
    def history(self):
        """当前生效的生成历史库。"""
        return self._history

    @history.setter
    def history(self, value):
        self._history = value

    def _history_getter(self):
        """当前历史库；插件入口替换 history_store 时这里跟着变。"""
        return self._history

    def set_history(self, store) -> None:
        """替换历史库（配置重载 / 测试）。"""
        self._history = store

    # ------------------------------------------------------------------ 在途请求
    def enter_request(self) -> None:
        """标记一条消息开始处理。"""
        self._active_requests += 1

    async def exit_request(self) -> None:
        """消息处理结束；没人用了才去关那些被替换下来的旧连接。"""
        self._active_requests = max(0, self._active_requests - 1)
        if self._active_requests or not self._retired:
            return
        retired, self._retired = self._retired, []
        for rt in retired:
            try:
                await rt.close()
            except Exception:  # noqa: BLE001
                logger.debug("[neko_draw] 关闭旧运行组件失败", exc_info=True)

    async def _retire_runtime(self, rt) -> None:
        """重载后回收旧运行组件：没人正在用就立刻关，否则等最后一个请求结束。"""
        if self._active_requests > 0:
            self._retired.append(rt)
            logger.info("[neko_draw] 仍有 %d 个请求在处理，旧连接会在它们结束后回收",
                        self._active_requests)
            return
        await rt.close()

    # ------------------------------------------------------------------ 重载
    async def reload(self) -> None:
        """保存 WebUI 后原子替换运行组件，让所有设置无需重启即可生效。"""
        old = self.runtime
        new = RuntimeServices.build(
            self.conf,
            context=self.context,
            data_dir=self.data_dir,
            save_dir=self.save_dir,
            plugin_dir=self.plugin_dir,
            previous=old,
        )
        try:
            self.runtime = new
            self._build_services()
        except Exception:
            # 装配失败：先把运行时换回去，保证插件继续可用。
            # 注意：WebUI 保存配置时会同步把内存配置回滚成旧值；
            # 这里不要再拿"新旧夹杂"的配置重建服务，交给调用方的回滚 +
            # 下一次 reload 处理即可，避免二次失败把状态搞得更乱。
            self.runtime = old
            await new.close()
            logger.exception("[neko_draw] 配置应用失败，运行组件已保持为上一份配置")
            raise
        await self._retire_runtime(old)
        await asyncio.to_thread(
            self.history.enforce_limit, max(0, get_int(self.conf, "drawing_history_limit", 200))
        )
        await asyncio.to_thread(
            self.apng_history.enforce_limit, max(0, get_int(self.conf, "apng_history_limit", 200))
        )

    # ------------------------------------------------------------------ 存储
    def command_stores(self) -> dict:
        """扩写 / 提取两个分区当前生效的存储实例（供 WebUI 读写）。"""
        return {
            "expand": self.runtime.expand_store,
            "extract": self.runtime.extract_store,
        }

    # ------------------------------------------------------------------ 调用历史
    def record_llm_call(self, event, outcome, item: dict, result: str) -> None:
        """把一次扩写/提取调用写入 llm_history.db；失败不抛给用户。"""
        store = getattr(self.runtime, "llm_history", None)
        if store is None:
            return
        try:
            store.add(
                section=outcome.section,
                trigger_word=str(item.get("trigger", "") or ""),
                user_id=str(getattr(event, "get_sender_id", lambda: "")() or ""),
                group_id=str(getattr(event, "get_group_id", lambda: "")() or ""),
                umo=str(getattr(event, "unified_msg_origin", "") or ""),
                provider=str(outcome.meta.get("provider_id") or ""),
                model=outcome.model,
                timeout_total=float(get_int(self.conf, f"{outcome.section}_global_timeout", 60)),
                duration_ms=float(outcome.duration_ms or 0),
                status="success" if result else "failed",
                error_message="" if result else str(outcome.reason or "未知原因"),
                input_text=outcome.body,
                system_prompt=outcome.system_prompt,
                prompt=outcome.template,
                output_text=result,
                image_count=int(outcome.image_count),
                image_source=outcome.image_source,
                source=str(getattr(outcome, "source", "") or "user"),
            )
            limit = get_int(self.conf, "llm_history_limit", 500)
            if limit > 0:
                store.enforce_limit(limit)
        except Exception:  # noqa: BLE001
            logger.debug("[neko_draw] 写入调用历史失败", exc_info=True)

    # ------------------------------------------------------------------ 退出
    async def shutdown(self) -> None:
        await self.pending.shutdown()
        await self.runtime.close()
