# -*- coding: utf-8 -*-
"""插件配置访问的统一入口。

配置对象在运行期可能是 ``AstrBotConfig``（dict 子类，带 ``save_config``），
也可能在测试里被换成普通 dict。所有模块都通过这里读配置，避免各处自己写
``self.conf.get(...)`` 时对默认值处理不一致。
"""
from __future__ import annotations

from typing import Any

from astrbot.api import logger


def get(config: Any, key: str, default: Any = None) -> Any:
    """读一个配置项；缺失返回默认值（不会把 None 当成“用户显式关闭”）。"""
    try:
        if config is None:
            return default
        if hasattr(config, "get"):
            value = config.get(key, default)
        else:
            value = getattr(config, key, default)
    except Exception:  # noqa: BLE001 - 配置对象异常时不能拖垮消息处理
        return default
    return default if value is None else value


def get_str(config: Any, key: str, default: str = "") -> str:
    value = get(config, key, default)
    return str(value).strip() if value is not None else default


def get_int(config: Any, key: str, default: int = 0) -> int:
    """读整数；空字符串 / None / 无法解析时回落默认值。

    注意：真正写 0 仍然返回 0（0 不是“空值”），只有空值才回落默认。
    """
    value = get(config, key, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_float(config: Any, key: str, default: float = 0.0) -> float:
    value = get(config, key, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_bool(config: Any, key: str, default: bool = False) -> bool:
    value = get(config, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on", "开", "是"):
            return True
        if low in ("0", "false", "no", "off", "关", "否", ""):
            return False
    return bool(default)


def get_list(config: Any, key: str, default: Any = None) -> list:
    value = get(config, key, default if default is not None else [])
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def as_dict(config: Any) -> dict:
    """把配置对象安全地转成普通 dict（用于快照 / 回滚）。"""
    if config is None:
        return {}
    try:
        if hasattr(config, "items"):
            return {str(k): v for k, v in config.items()}
    except Exception:  # noqa: BLE001
        pass
    if isinstance(config, dict):
        return dict(config)
    return dict(getattr(config, "__dict__", {}) or {})


def persist(config: Any) -> None:
    """尽力把配置写盘；配置对象不支持时静默跳过（命令里改配置的场景）。"""
    saver = getattr(config, "save_config", None)
    if not callable(saver):
        return
    try:
        saver()
    except Exception:  # noqa: BLE001
        # 命令里改完配置：写盘失败只影响“重启后保留”，不该让指令本身报错
        logger.debug("[neko_draw] 配置写盘失败", exc_info=True)
