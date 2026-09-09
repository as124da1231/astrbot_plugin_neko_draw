# -*- coding: utf-8 -*-
"""RunningHub API 客户端（提交 + 轮询模式）。

协议：
  1. POST {base_url}/{model}     -> 返回 {taskId, status, results: null}
  2. POST {base_url}/query       body {taskId} -> {status, results: [{url}, ...]}

模型名直接拼在 URL 路径中（如 seedream-v5-lite/text-to-image）。
状态：QUEUED / RUNNING / SUCCESS / FAILED。
参考图字段由模板 refer_field 决定（如 imageUrls），支持公开 URL 或 Base64 data URI。
"""
import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("neko_draw")

TERMINAL_FAILED = ("FAILED",)


class RunningHubError(Exception):
    pass


class RunningHubClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://www.runninghub.cn/openapi/v2",
        proxy: Optional[str] = None,
        poll_interval: float = 2.0,
        timeout: float = 300,
        max_concurrency: int = 2,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_concurrency = max(1, int(max_concurrency))
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
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def submit(self, model: str, payload: dict) -> str:
        """提交生成任务，返回 taskId。"""
        url = f"{self.base_url}/{model}"
        try:
            async with self.session.post(
                url, json=payload, headers=self._headers(), proxy=self.proxy
            ) as resp:
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise RunningHubError(f"提交请求失败: {e}") from e
        if resp.status != 200:
            raise RunningHubError(f"提交失败 HTTP {resp.status}: {data}")
        task_id = data.get("taskId")
        if not task_id:
            raise RunningHubError(f"响应中未找到 taskId: {data}")
        # 提交即成功（极少数情况），直接返回，poll 会处理
        return task_id

    async def poll(self, task_id: str) -> list[str]:
        """轮询直到终态，返回 results 中的 url 列表。"""
        url = f"{self.base_url}/query"
        while True:
            try:
                async with self.session.post(
                    url, json={"taskId": task_id},
                    headers=self._headers(), proxy=self.proxy,
                ) as resp:
                    data = await resp.json()
            except aiohttp.ClientError as e:
                raise RunningHubError(f"查询请求失败: {e}") from e
            if resp.status != 200:
                raise RunningHubError(f"查询失败 HTTP {resp.status}: {data}")
            status = str(data.get("status", "")).upper()
            if status == "SUCCESS":
                results = data.get("results") or []
                urls = []
                for item in results:
                    if isinstance(item, dict):
                        u = item.get("url")
                        if u and isinstance(u, str) and u.strip():
                            urls.append(u)
                return urls
            if status in TERMINAL_FAILED:
                err = data.get("errorMessage") or data.get("errorCode") or status
                raise RunningHubError(f"任务{status}: {err}")
            # QUEUED / RUNNING / 其他未知状态 → 继续等
            await asyncio.sleep(self.poll_interval)

    async def generate(self, model: str, payload: dict) -> list[str]:
        """提交并等待完成，返回图片 URL 列表。受信号量约束。轮询受整体 timeout 约束。"""
        async with self._semaphore:
            task_id = await self.submit(model, payload)
            try:
                return await asyncio.wait_for(
                    self.poll(task_id), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                raise RunningHubError(
                    f"任务轮询超时（{self.timeout}秒），任务ID: {task_id}"
                )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
