# -*- coding: utf-8 -*-
"""图片下载与格式转换。

- 把消息中的图片（URL / 本地路径 / data URI）转成 WaveSpeed edit 模式
  需要的 data URI（编辑接口参考图入参格式）
- 把生成结果 URL 下载到本地，供 AstrBot 发送
"""
import base64
import mimetypes
import os
import re
import uuid
import asyncio
import logging
from pathlib import Path
from typing import Optional

import aiohttp

_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+);base64,(?P<data>.+)$", re.S)
logger = logging.getLogger("neko_draw")


class Downloader:
    def __init__(self, save_dir: str | Path, proxy: Optional[str] = None):
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.proxy = proxy
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    def _ext(self, mime: str) -> str:
        return {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime.split(";")[0].lower(), ".png")

    @staticmethod
    def _detect_image_mime(raw: bytes) -> Optional[str]:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "image/webp"
        return None

    def to_data_uri(self, url_or_path: str) -> Optional[str]:
        """把 URL / 本地路径 / data URI 统一转成 data URI（WaveSpeed edit 入参）。"""
        m = _DATA_URI_RE.match(url_or_path)
        if m:
            return url_or_path
        if url_or_path.startswith(("http://", "https://")):
            raise ValueError("远程图片需要先下载，请调用 download_to_data_uri")
        if os.path.isfile(url_or_path):
            ext = os.path.splitext(url_or_path)[1].lower()
            mime = mimetypes.guess_type(url_or_path)[0] or (
                "image/png" if ext in (".png", ".mpo") else "image/jpeg"
            )
            with open(url_or_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:{mime};base64,{b64}"
        return None

    async def download_to_data_uri(self, url: str) -> Optional[str]:
        """下载远程图片并转为 data URI（本地路径直接读取）。"""
        if _DATA_URI_RE.match(url):
            return url
        if url.startswith('file://'):
            from urllib.parse import unquote, urlparse
            url = unquote(urlparse(url).path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:', url):
                url = url[1:]
        if os.path.isfile(url):
            return self.to_data_uri(url)
        try:
            async with self.session.get(url, proxy=self.proxy) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        mime = resp.headers.get("Content-Type", "image/png").split(";")[0]
        if not mime.startswith("image/"):
            return None
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def _save_data_uri(self, data_uri: str) -> Optional[str]:
        """把 data URI 解码并保存为本地文件，返回路径。"""
        m = _DATA_URI_RE.match(data_uri)
        if not m:
            return None
        mime = m.group("mime")
        try:
            raw = base64.b64decode(m.group("data"))
        except (base64.binascii.Error, ValueError):
            return None
        path = self.save_dir / f"img_{uuid.uuid4().hex[:12]}{self._ext(mime)}"
        path.write_bytes(raw)
        return str(path)

    async def download(self, url: str, *, proxy: Optional[str] = None) -> Optional[str]:
        """下载图片到本地文件，返回路径。支持 http(s) URL 和 data URI。"""
        if url.startswith("data:"):
            return self._save_data_uri(url)
        try:
            async with self.session.get(url, proxy=proxy if proxy is not None else self.proxy, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0 NekoDraw/1.0"}) as resp:
                if resp.status != 200:
                    logger.warning("生成图片下载失败 HTTP %s: %s", resp.status, str(url)[:240])
                    return None
                raw = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            logger.warning("生成图片下载异常 %s: %s", type(exc).__name__, str(url)[:240])
            return None
        declared = resp.headers.get("Content-Type", "").split(";")[0].lower()
        detected = self._detect_image_mime(raw)
        mime = declared if declared.startswith("image/") else detected
        if not mime:
            logger.warning("生成结果不是图片（Content-Type=%s，大小=%s）: %s", declared or "unknown", len(raw), str(url)[:240])
            return None
        path = self.save_dir / f"img_{uuid.uuid4().hex[:12]}{self._ext(mime)}"
        path.write_bytes(raw)
        return str(path)

    async def materialize_image(self, url_or_path: str) -> Optional[str]:
        """将消息中的图片引用落地为本地文件。

        本地路径直接返回；file URI、data URI 和远程 URL 会被统一处理。
        """
        value = str(url_or_path or "").strip()
        if not value:
            return None
        if value.startswith("file://"):
            from urllib.parse import unquote, urlparse
            value = unquote(urlparse(value).path)
            if os.name == "nt" and re.match(r"^/[A-Za-z]:", value):
                value = value[1:]
        if os.path.isfile(value):
            return str(Path(value).resolve())
        data_uri = await self.download_to_data_uri(value)
        if not data_uri:
            return None
        return self._save_data_uri(data_uri)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
