# -*- coding: utf-8 -*-
"""从 AstrBot 消息链中提取图片引用（URL / 本地路径）。

支持以下图片来源：
- 引用（Reply）消息中的图片：Reply.chain 里的 Image
- 当前消息中的 Image 组件
- File 组件（图片扩展名 / http 链接）

组件类型通过参数注入，避免 core 层依赖 AstrBot，便于独立测试。
"""
from typing import Optional

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".mpo"}


def _component_ref(component, attrs: tuple[str, ...]) -> Optional[str]:
    """返回组件上第一个非空的媒体引用字段。"""
    for attr in attrs:
        value = getattr(component, attr, None)
        if value:
            return str(value)
    return None


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

    def walk(chain) -> None:
        for comp in chain:
            if reply_types and isinstance(comp, reply_types):
                sub = getattr(comp, "chain", None)
                if sub:
                    walk(sub)
                continue
            if isinstance(comp, image_types):
                ref = _component_ref(comp, ("url", "file", "path"))
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

    walk(components)
    return urls
