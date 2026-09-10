# -*- coding: utf-8 -*-
"""从 AstrBot 消息链中提取图片引用（URL / 本地路径）。

支持以下图片来源：
- 引用（Reply）消息中的图片：Reply.chain 里的 Image
- 当前消息中的 Image 组件
- File 组件（图片扩展名 / http 链接）

组件类型通过参数注入，避免 core 层依赖 AstrBot，便于独立测试。
"""
import os
from typing import Optional

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".mpo"}


def _component_ref(component, attrs: tuple[str, ...]) -> Optional[str]:
    """优先返回可直接读取的本地/data 引用，再使用临时网络 URL。"""
    candidates: list[tuple[str, str]] = []
    for attr in attrs:
        value = getattr(component, attr, None)
        if value:
            candidates.append((attr, str(value)))
    for _attr, value in candidates:
        if value.startswith("data:") or value.startswith("file://") or os.path.isfile(value):
            return value
    # 多个网络地址并存时优先 Image.url；Image.file 有时只是 QQ 文件 ID，
    # 也可能是兼容层生成的临时地址。
    for attr, value in candidates:
        if attr == "url" and value.startswith(("http://", "https://")):
            return value
    for _attr, value in candidates:
        if value.startswith(("http://", "https://")):
            return value
    return candidates[0][1] if candidates else None


def extract_image_urls(
    components: list,
    *,
    image_types: tuple[type, ...],
    reply_types: tuple[type, ...] = (),
    file_types: tuple[type, ...] = (),
    image_exts: Optional[set[str]] = None,
) -> list[str]:
    """按消息链顺序提取图片引用。

    - image_types: Image 组件类型（取 url/file/path 第一个非空）
    - reply_types: Reply 组件类型，其 chain 会被递归遍历
    - file_types: File 组件类型，仅收集图片扩展名或 http(s) 链接
    """
    exts = image_exts or IMAGE_EXTS
    urls: list[str] = []

    visited: set[int] = set()

    def walk(chain) -> None:
        if chain is None or isinstance(chain, (str, bytes)):
            return
        marker = id(chain)
        if marker in visited:
            return
        visited.add(marker)
        if not isinstance(chain, (list, tuple)):
            chain = [chain]
        for comp in chain:
            if reply_types and isinstance(comp, reply_types):
                sub = getattr(comp, "chain", None)
                if sub:
                    walk(sub)
                continue
            if isinstance(comp, image_types):
                ref = _component_ref(comp, ("file", "path", "url"))
                if ref:
                    urls.append(ref)
                continue
            if file_types and isinstance(comp, file_types):
                ref = _component_ref(comp, ("url", "file_", "file", "path"))
                if not ref:
                    continue
                name = str(getattr(comp, "name", "") or "")
                ref_lower = ref.lower()
            # 仅收集以常见图片扩展名结尾的 URL 或文件名
                if ref_lower.endswith(tuple(exts)) or name.lower().endswith(
                    tuple(exts)
                ):
                    urls.append(ref)
                continue
            # AstrBot 已展开的合并转发通常是 Node/Nodes；避免在 core 层
            # 绑定具体组件版本，按公共容器字段递归读取。
            for attr in ("chain", "content", "nodes", "message", "messages"):
                child = getattr(comp, attr, None)
                if child is not None:
                    walk(child)

    walk(components)
    return urls
