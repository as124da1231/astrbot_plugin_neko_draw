# -*- coding: utf-8 -*-
"""``/nc`` 管理指令的实现。

指令声明留在 ``main.py``（AstrBot 需要装饰器在类上声明），具体动作在这里，
方便单独测试，也让 ``main.py`` 保持只有声明没有逻辑。
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from .config_store import persist
from .objects import remove_files
from .text_utils import split_command

# /nc fs 单张图下载上限（秒）：超过就给一句能看懂的提示，而不是静默没反应
FIRST_FRAME_TIMEOUT_SECONDS = 60

HELP_TEXT = (
    "Neko Draw 指令\n"
    "绘图：nd <提示词>\n编辑：nde <提示词> + 图片\n"
    "动画：apng [间隔秒数] [循环次数] [--fd 首帧秒数] + 图片\n"
    "只输入 apng 时使用 WebUI 默认间隔（默认 5 秒）\n\n"
    "管理员 /nc 子指令\n"
    "pa/pd/pl/ps：添加、删除、列出、查看预设\n"
    "ea/ed/el：扩写指令的添加、删除、列出\n"
    "ta/td/tl：形象提取指令的添加、删除、列出\n"
    "wa/wd/wl：添加、删除、列出白名单（u=用户，g=群组）\n"
    "fs/fv/fr：设置、查看、恢复 APNG 首帧（回复一张图再发 /nc fs 也行）\n"
    "ao/af：开启、关闭生成图片的 APNG 包装\n\n"
    "提示：白名单要先在 WebUI 打开「启用白名单」才生效"
)


def _reply(event: Any, text: str):
    return event.plain_result(str(text))


def _ok(flag: bool, message: str) -> str:
    return f"{'✅' if flag else '❌'} {message}"


def _not_found(label: str, trigger: str, triggers: list[str], detail_cmd: str = "") -> str:
    """找不到触发词的统一回复：说清楚“没有”，并列出“有哪些”。"""
    head = f"❌ 没有找到{label}「{trigger}」"
    if not triggers:
        return f"{head}，当前一个都没有。"
    listing = "、".join(triggers[:30]) + ("…" if len(triggers) > 30 else "")
    tail = f"；用 /nc {detail_cmd} <触发词> 查看模板" if detail_cmd else ""
    return f"{head}。现有：{listing}{tail}"


async def command_router(plugin: Any, action: str, event: Any, *args) -> AsyncGenerator:
    """把 ``/nc`` 子指令路由到对应实现；未识别的动作静默忽略。"""
    handler = _ACTIONS.get(action)
    if handler is None:
        return
    async for item in handler(plugin, event, *args):
        yield item


def _store(plugin, section: str):
    """取扩写/提取存储；接口异常时返回 None，由调用方给出可读提示。"""
    try:
        stores = plugin._command_stores() or {}
    except Exception:  # noqa: BLE001
        logger.warning("[neko_draw] 读取指令存储失败", exc_info=True)
        return None
    if not isinstance(stores, dict):
        return None
    return stores.get(section)


# ---------------------------------------------------------------- 帮助
async def _help(plugin, event):
    yield _reply(event, HELP_TEXT)


# ---------------------------------------------------------------- 预设提示词
async def _add_prompt(plugin, event):
    rest = split_command(event.message_str, ["nc pa"]) or ""
    parts = rest.split(None, 1)
    if len(parts) < 2:
        yield _reply(event, "❌ 用法：/nc pa <触发词> <提示词内容>")
        return
    ok, msg = plugin.prompt_manager.add(parts[0], parts[1])
    yield _reply(event, _ok(ok, msg))


async def _del_prompt(plugin, event, trigger_word=""):
    trigger_word = str(trigger_word or "").strip()
    if not trigger_word:
        yield _reply(event, "❌ 用法：/nc pd <触发词>")
        return
    ok, msg = plugin.prompt_manager.delete(trigger_word)
    if ok:
        yield _reply(event, f"🗑️ {msg}")
        return
    yield _reply(event, _not_found("预设", trigger_word, plugin.prompt_manager.list_prompts(), "ps"))


async def _list_prompts(plugin, event):
    triggers = plugin.prompt_manager.list_prompts()
    if not triggers:
        yield _reply(event, "📜 当前没有预设提示词。")
        return
    yield _reply(event, "📜 当前预设提示词列表：\n" + "、".join(triggers))


async def _prompt_details(plugin, event, trigger_word=""):
    trigger_word = str(trigger_word or "").strip()
    if not trigger_word:
        yield _reply(event, "❌ 用法：/nc ps <触发词>")
        return
    detail = plugin.prompt_manager.get_detail(trigger_word)
    if detail is None:
        yield _reply(event, _not_found("预设", trigger_word, plugin.prompt_manager.list_prompts()))
        return
    yield _reply(event, f"📋 提示词详情：「{trigger_word}」\n{detail}")


# ---------------------------------------------------------------- 扩写 / 提取
async def _add_section(plugin, event, section: str, command: str, label: str):
    rest = split_command(event.message_str, [command]) or ""
    parts = rest.split(None, 1)
    if len(parts) < 2:
        yield _reply(event, f"❌ 用法：/{command} <触发词> <提示词模板>")
        return
    store = _store(plugin, section)
    if store is None:
        yield _reply(event, f"❌ {label}存储尚未初始化，请稍后重试")
        return
    ok, msg = store.upsert({"trigger": parts[0], "prompt": parts[1]})
    yield _reply(event, _ok(ok, msg))


async def _del_section(plugin, event, section: str, trigger_word: str, label: str, usage_cmd: str = "ed"):
    store = _store(plugin, section)
    if store is None:
        yield _reply(event, f"❌ {label}存储尚未初始化，请稍后重试")
        return
    trigger_word = str(trigger_word or "").strip()
    if not trigger_word:
        yield _reply(event, f"❌ 用法：/nc {usage_cmd} <触发词>")
        return
    ok, msg = store.delete(trigger_word)
    if ok:
        yield _reply(event, f"🗑️ {msg}")
        return
    # 没删掉：告诉用户现在有哪些（只说现有的，不猜命令名）
    yield _reply(event, _not_found(label, trigger_word, store.triggers()))


async def _list_section(plugin, event, section: str, empty: str, title: str):
    store = _store(plugin, section)
    triggers = store.triggers() if store is not None else []
    if not triggers:
        yield _reply(event, empty)
        return
    yield _reply(event, f"{title}\n" + "、".join(triggers))


async def _add_expand(plugin, event):
    async for item in _add_section(plugin, event, "expand", "nc ea", "扩写指令"):
        yield item


async def _del_expand(plugin, event, trigger_word=""):
    async for item in _del_section(plugin, event, "expand", trigger_word, "扩写指令", "ed"):
        yield item


async def _list_expand(plugin, event):
    async for item in _list_section(plugin, event, "expand", "📜 当前没有扩写指令。", "📜 当前扩写指令："):
        yield item


async def _add_extract(plugin, event):
    async for item in _add_section(plugin, event, "extract", "nc ta", "形象提取指令"):
        yield item


async def _del_extract(plugin, event, trigger_word=""):
    async for item in _del_section(plugin, event, "extract", trigger_word, "形象提取指令", "td"):
        yield item


async def _list_extract(plugin, event):
    async for item in _list_section(plugin, event, "extract", "📜 当前没有形象提取指令。", "📜 当前形象提取指令："):
        yield item


# ---------------------------------------------------------------- 白名单
_WHITELIST_KINDS = {"u": "用户", "g": "群组", "用户": "用户", "群组": "群组"}


def _whitelist_args(cmd_type: Any, target_id: Any) -> tuple[str, str]:
    """把 ``u/g`` 归一成 ``用户/群组``，并把 ID 转成字符串。"""
    kind = _WHITELIST_KINDS.get(str(cmd_type or "").strip().lower(), "")
    return kind, str(target_id or "").strip()


async def _add_whitelist(plugin, event, cmd_type="", target_id=""):
    kind, target = _whitelist_args(cmd_type, target_id)
    if not kind or not target:
        # 以前这两种情况直接回一句“用法”，看不出是哪个参数错了
        wrong = "类型" if not kind else "ID"
        yield _reply(event, f"❌ {wrong}没填对。用法：/nc wa <u|g> <ID>（u=用户，g=群组）")
        return
    ok, msg = plugin.whitelist_guard.add(kind, target)
    yield _reply(event, _ok(ok, msg))
    # 加成功但总开关没开时，用户会以为“加了却没生效”，这里说明一句
    if ok and not getattr(plugin.whitelist_guard, "enabled", False):
        yield _reply(event, "提示：「启用白名单」开关目前是关的，加进去也不会限制谁；需要时再打开")


async def _del_whitelist(plugin, event, cmd_type="", target_id=""):
    kind, target = _whitelist_args(cmd_type, target_id)
    if not kind or not target:
        wrong = "类型" if not kind else "ID"
        yield _reply(event, f"❌ {wrong}没填对。用法：/nc wd <u|g> <ID>（u=用户，g=群组）")
        return
    ok, msg = plugin.whitelist_guard.delete(kind, target)
    if ok:
        yield _reply(event, f"🗑️ {msg}")
        return
    yield _reply(event, f"❌ {msg}（可用 /nc wl 查看现有白名单）")


async def _list_whitelist(plugin, event):
    yield _reply(event, plugin.whitelist_guard.list_whitelist())


# ---------------------------------------------------------------- APNG 首帧
async def _set_first_frame(plugin, event):
    # 优先用本条消息里的图；没有就看引用的消息（“回复一张图再发 /nc fs”）
    refs = plugin.app.collector.message_refs(event)
    if not refs:
        refs = await plugin.app.collector.quoted_refs(event)
    if not refs:
        yield _reply(event, "❌ 请把图片和 /nc fs 一起发，或先回复那张图片再发 /nc fs")
        return
    local = None
    try:
        local = await asyncio.wait_for(
            plugin.app.collector.materialize(event, refs[-1]),
            timeout=FIRST_FRAME_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("[neko_draw] 首帧图片下载超时")
    except Exception:  # noqa: BLE001
        logger.warning("[neko_draw] 首帧图片解析失败", exc_info=True)
    if not local:
        yield _reply(
            event,
            f"❌ 首帧图片下载失败或超时（超过 {FIRST_FRAME_TIMEOUT_SECONDS} 秒），请换一张再试",
        )
        return
    target = None
    try:
        target = await asyncio.to_thread(plugin.output.save_first_frame, local)
        plugin.conf["apng_first_frame_path"] = [str(target)]
        persist(plugin.conf)
    except Exception:  # noqa: BLE001
        logger.warning("[neko_draw] 首帧保存失败", exc_info=True)
    finally:
        # 下载下来的聊天图片是已另存的临时副本，存好就删，避免留在 save_images 里
        try:
            if plugin.app.collector._is_temporary(local):
                await asyncio.to_thread(remove_files, [local])
        except Exception:  # noqa: BLE001
            logger.debug("[neko_draw] 清理首帧临时图失败", exc_info=True)
    if target is None:
        yield _reply(event, "❌ 首帧保存失败")
        return
    yield _reply(event, "✅ APNG 首帧已更新")


async def _view_first_frame(plugin, event):
    path = plugin.output._resolve_apng_first_frame()
    if path is None:
        yield _reply(event, "❌ 当前没有可用的 APNG 首帧")
        return
    yield event.chain_result([Plain("当前 APNG 首帧："), Image.fromFileSystem(str(path))])


async def _reset_first_frame(plugin, event):
    plugin.conf["apng_first_frame_path"] = []
    persist(plugin.conf)
    yield _reply(event, "✅ 已恢复插件内置 APNG 首帧")


async def _enable_apng(plugin, event):
    plugin.conf["enable_apng_wrap"] = True
    persist(plugin.conf)
    yield _reply(event, "✅ 生成图片的 APNG 首帧包装已开启")


async def _disable_apng(plugin, event):
    plugin.conf["enable_apng_wrap"] = False
    persist(plugin.conf)
    yield _reply(event, "✅ 生成图片的 APNG 首帧包装已关闭")


_ACTIONS = {
    "help": _help,
    "add_prompt": _add_prompt,
    "del_prompt": _del_prompt,
    "list_prompts": _list_prompts,
    "prompt_details": _prompt_details,
    "add_expand": _add_expand,
    "del_expand": _del_expand,
    "list_expand": _list_expand,
    "add_extract": _add_extract,
    "del_extract": _del_extract,
    "list_extract": _list_extract,
    "add_whitelist": _add_whitelist,
    "del_whitelist": _del_whitelist,
    "list_whitelist": _list_whitelist,
    "set_first_frame": _set_first_frame,
    "view_first_frame": _view_first_frame,
    "reset_first_frame": _reset_first_frame,
    "enable_apng": _enable_apng,
    "disable_apng": _disable_apng,
}
