# -*- coding: utf-8 -*-
"""解析合并转发结构中的图片和转发 ID。"""
from typing import Any


FORWARD_TYPES = {"forward", "forward_msg", "forward_message"}


def _children(value: Any):
    if isinstance(value, dict):
        yield from value.values()
    elif isinstance(value, (list, tuple)):
        yield from value
    else:
        for name in ("chain", "content", "nodes", "message", "messages"):
            child = getattr(value, name, None)
            if child is not None:
                yield child


def extract_forward_ids(value: Any, max_depth: int = 12) -> list[str]:
    found: list[str] = []
    seen: set[int] = set()

    def walk(item: Any, depth: int) -> None:
        if item is None or depth > max_depth or isinstance(item, (str, bytes, int, float, bool)):
            return
        marker = id(item)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(item, dict):
            kind = str(item.get("type", "")).lower()
            data = item.get("data") if isinstance(item.get("data"), dict) else item
            if kind in FORWARD_TYPES:
                for key in ("id", "forward_id", "message_id", "res_id", "resid"):
                    value = data.get(key)
                    if value and str(value) not in found:
                        found.append(str(value))
        else:
            name = item.__class__.__name__.lower()
            if "forward" in name:
                for key in ("id", "forward_id", "message_id", "res_id", "resid"):
                    value = getattr(item, key, None)
                    if value and str(value) not in found:
                        found.append(str(value))
        for child in _children(item):
            walk(child, depth + 1)

    walk(value, 0)
    return found


def extract_payload_images(value: Any, max_depth: int = 20) -> list[str]:
    found: list[str] = []
    seen: set[int] = set()

    def walk(item: Any, depth: int) -> None:
        if item is None or depth > max_depth or isinstance(item, (str, bytes, int, float, bool)):
            return
        marker = id(item)
        if marker in seen:
            return
        seen.add(marker)
        if isinstance(item, dict):
            kind = str(item.get("type", "")).lower()
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            if kind == "image":
                for key in ("url", "file", "path"):
                    ref = data.get(key)
                    if ref:
                        found.append(str(ref))
                        break
        for child in _children(item):
            walk(child, depth + 1)

    walk(value, 0)
    return found
