# -*- coding: utf-8 -*-
"""OpenAI 兼容格式图像生成客户端（适用所有 OpenAI 兼容 API）。

协议（同步，无需轮询）：
  POST {base_url}/{endpoint}   body: {model, prompt, ...模板params}
  响应: {data: [{url | b64_json}, ...]}

默认端点 images/generations，可在模板 endpoint 字段覆盖。
模型名放入请求体（不拼 URL），适配 OpenAI / SiliconFlow / 各类中转。
"""
import asyncio
import base64
import logging
from typing import Mapping, Optional

import aiohttp

logger = logging.getLogger("neko_draw")

DEFAULT_ENDPOINT = "images/generations"


class OpenAPIError(Exception):
    pass


class OpenAPIClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        proxy: Optional[str] = None,
        timeout: float = 300,
        max_concurrency: int = 2,
        custom_headers: Optional[Mapping[str, str]] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.timeout = timeout
        self.max_concurrency = max(1, int(max_concurrency))
        self.custom_headers = {
            str(key): str(value) for key, value in (custom_headers or {}).items()
        }
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        headers.update(self.custom_headers)
        return headers

    async def generate(self, model: str, payload: dict) -> list[str]:
        """提交生成并返回图片 URL 列表（同步接口，无需轮询）。

        model 放入请求体；端点从 payload 的 _endpoint 取（弹出），默认 images/generations。
        """
        endpoint = payload.pop("_endpoint", DEFAULT_ENDPOINT)
        payload["model"] = model
        url = f"{self.base_url}/{endpoint}"
        async with self._semaphore:
            try:
                async with self.session.post(
                    url, json=payload, headers=self._headers(), proxy=self.proxy
                ) as resp:
                    data = await resp.json()
            except aiohttp.ClientError as e:
                raise OpenAPIError(f"请求失败: {e}") from e
            if resp.status != 200:
                raise OpenAPIError(f"HTTP {resp.status}: {data}")
        return self._extract_urls(data)

    @staticmethod
    def _extract_urls(data: dict) -> list[str]:
        """兼容 OpenAI 的 data 与 SiliconFlow 的 images 响应结构。"""
        items = data.get("data") or data.get("images") or data.get("output") or []
        if isinstance(items, dict):
            items = items.get("images") or items.get("data") or [items]
        urls: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                if isinstance(item, str) and item.strip():
                    urls.append(item.strip())
                continue
            url = item.get("url") or item.get("image_url")
            encoded = item.get("b64_json") or item.get("base64") or item.get("b64")
            if isinstance(url, dict):
                url = url.get("url")
            if url:
                urls.append(str(url))
            elif encoded:
                urls.append(f"data:image/png;base64,{encoded}")
        if not urls and data.get("url"):
            # 某些兼容平台直接返回 {url: "..."}
            urls.append(str(data["url"]))
        return urls

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
