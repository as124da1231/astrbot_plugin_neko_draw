# -*- coding: utf-8 -*-
"""多 Key 顺序回退辅助。

一个提供商可配置“主 Key + 若干备用 Key”。每次请求按顺序使用：先用主 Key，
当它触发“换 Key 就能救”的错误时，自动切到下一个备用 Key 重试。

可切换的错误包括：余额不足、Key 失效 / 未授权、并发或频率超限、网络错误、
服务端 5xx。工作流参数错误、节点错误等换 Key 也无效的情况不切换。

对外只给出中文说明，不暴露错误码。
"""
from __future__ import annotations

from typing import Awaitable, Callable, Iterable, Optional


def normalize_keys(api_key: object = "", api_keys: object = None) -> list[str]:
    """把主 Key 与备用 Key 合并为去重后的有序列表（主 Key 在最前）。"""
    candidates: list[object] = [api_key]
    if isinstance(api_keys, (list, tuple)):
        candidates.extend(api_keys)
    elif api_keys:
        candidates.append(api_keys)
    keys: list[str] = []
    for item in candidates:
        text = str(item or "").strip()
        if text and text not in keys:
            keys.append(text)
    return keys


class KeySwitchError(Exception):
    """当前 Key 触发了可切换的错误，调用方应换下一个 Key 重试。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def is_switchable_status(status: object) -> bool:
    """按 HTTP 状态码判断是否值得换 Key：认证 / 余额 / 限流 / 服务端错误。"""
    try:
        code = int(status)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return code in (401, 402, 403, 429) or code >= 500


async def run_with_keys(
    keys: Iterable[str],
    runner: Callable[[str], Awaitable[object]],
    logger=None,
    label: str = "请求",
):
    """按顺序用每个 Key 调用 ``runner(key)``；遇到 KeySwitchError 自动切换。

    ``runner`` 必须接受一个 Key 参数。全部 Key 都失败时抛出 KeySwitchError，
    对外消息取最后一个错误（不含错误码）。非切换类异常直接向上抛出，不换 Key。
    """
    ordered = [str(k or "").strip() for k in keys]
    ordered = [k for k in ordered if k] or [""]
    last: Optional[KeySwitchError] = None
    for index, key in enumerate(ordered):
        try:
            return await runner(key)
        except KeySwitchError as exc:
            last = exc
            if index < len(ordered) - 1:
                if logger is not None:
                    logger.warning(
                        "[IMAGE] %s 使用第 %d/%d 个 Key 失败，自动切换下一个：%s",
                        label, index + 1, len(ordered), exc.message,
                    )
                continue
    raise last or KeySwitchError(f"{label}失败")
