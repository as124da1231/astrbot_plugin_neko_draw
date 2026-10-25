# -*- coding: utf-8 -*-
"""图片下载与格式转换。

- 把消息中的图片（URL / 本地路径 / data URI）转成 WaveSpeed edit 模式
  需要的 data URI（编辑接口参考图入参格式）
- 把生成结果 URL 下载到本地，供 AstrBot 发送
"""
import base64
import binascii
import mimetypes
import os
import re
import uuid
import asyncio
import logging
import html
from pathlib import Path
from typing import Optional

import aiohttp

_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;,]+);base64,(?P<data>.+)$", re.S)
logger = logging.getLogger("neko_draw")


class Downloader:
    def __init__(
        self,
        save_dir: str | Path,
        proxy: Optional[str] = None,
        *,
        retries: int = 3,
        retry_delay: float = 1.0,
        retry_backoff: float = 2.0,
    ):
        self.save_dir = Path(save_dir)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.proxy = proxy
        # 下载失败自动重试：retries 为「首次失败后额外重试次数」，0 表示不重试。
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.retry_backoff = max(1.0, float(retry_backoff))
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=120)
            )
        return self._session

    async def _sleep_backoff(self, attempt: int, label: str, reason: str) -> None:
        delay = self.retry_delay * (self.retry_backoff ** attempt)
        if delay > 0:
            logger.info("%s 第 %d 次重试，%.1fs 后重试（%s）", label, attempt + 1, delay, reason)
            await asyncio.sleep(delay)

    async def _fetch_bytes(
        self, url: str, *, proxy: Optional[str], label: str
    ) -> Optional[tuple[bytes, str]]:
        """下载 URL 原始字节，失败时按配置自动重试（指数退避）。

        返回 ``(raw, declared_content_type)``；重试耗尽仍失败返回 ``None``。
        """
        attempts = self.retries + 1
        for attempt in range(attempts):
            try:
                async with self.session.get(
                    url,
                    proxy=proxy if proxy is not None else self.proxy,
                    allow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0 NekoDraw/2.2"},
                ) as resp:
                    if resp.status != 200:
                        logger.warning("%s失败 HTTP %s: %s", label, resp.status, str(url)[:240])
                        if attempt < attempts - 1:
                            await self._sleep_backoff(attempt, label, f"HTTP {resp.status}")
                            continue
                        return None
                    raw = await resp.read()
                    declared = resp.headers.get("Content-Type", "").split(";")[0].lower()
                    return raw, declared
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                logger.warning("%s异常 %s: %s", label, type(exc).__name__, str(url)[:240])
                if attempt < attempts - 1:
                    await self._sleep_backoff(attempt, label, type(exc).__name__)
                    continue
                return None
        return None

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
        url = html.unescape(str(url or "").strip())
        if url.startswith("base64://"):
            return f"data:image/png;base64,{url[len('base64://') :]}"
        if _DATA_URI_RE.match(url):
            return url
        if url.startswith('file://'):
            from urllib.parse import unquote, urlparse
            url = unquote(urlparse(url).path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:', url):
                url = url[1:]
        if os.path.isfile(url):
            return self.to_data_uri(url)
        fetched = await self._fetch_bytes(url, proxy=self.proxy, label="参考图下载")
        if fetched is None:
            return None
        raw, declared = fetched
        detected = self._detect_image_mime(raw)
        mime = detected or (declared if declared.startswith("image/") else None)
        if not mime:
            logger.warning("参考图响应不是图片（Content-Type=%s，大小=%s）: %s", declared or "unknown", len(raw), str(url)[:240])
            return None
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def _save_data_uri(self, data_uri: str) -> Optional[str]:
        """把 data URI 解码并保存为本地文件，返回路径。"""
        m = _DATA_URI_RE.match(data_uri)
        if not m:
            return None
        mime = m.group("mime")
        try:
            # 先补齐 padding，再严格解码：脏数据直接返回 None，而不是解出乱码
            data = m.group("data")
            data += "=" * (-len(data) % 4)
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
        path = self.save_dir / f"img_{uuid.uuid4().hex[:12]}{self._ext(mime)}"
        path.write_bytes(raw)
        return str(path)

    async def download(self, url: str, *, proxy: Optional[str] = None) -> Optional[str]:
        """下载图片到本地文件，返回路径。支持 http(s) URL 和 data URI。"""
        if url.startswith("data:"):
            return self._save_data_uri(url)
        fetched = await self._fetch_bytes(url, proxy=proxy, label="生成图片下载")
        if fetched is None:
            return None
        raw, declared = fetched
        detected = self._detect_image_mime(raw)
        # 魔数优先：CDN 经常给错误页套 image/png，按声明存会把 HTML 当图片发出去
        mime = detected or (declared if declared.startswith("image/") else None)
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
