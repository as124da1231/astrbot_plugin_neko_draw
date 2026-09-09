# -*- coding: utf-8 -*-
"""WaveSpeed API 客户端（提交 + 轮询模式）。

调用流程：
  1. POST {base_url}/{model}             -> 返回任务 id（data.id）
  2. GET  {base_url}/predictions/{id}/result 轮询 -> data.outputs 为图片 URL 数组

模型名直接拼在 URL 路径中（WaveSpeed 的模型路由方式）。

并发控制：
- 套餐通常限制同时运行的预测数（如 2），插件内置信号量在提交前排队，
  避免"提交即失败"；
- 若外部环境（其他客户端/多实例）仍触发 429 并发限制，做指数退避重试。
"""
import asyncio
import logging
from typing import Awaitable, Callable, Optional

import aiohttp

logger = logging.getLogger("neko_draw")

TERMINAL_FAILED = ("failed", "cancelled", "timeout", "deleted", "error")


class WavespeedError(Exception):
    pass


class WavespeedClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.wavespeed.ai/api/v3",
        proxy: Optional[str] = None,
        poll_interval: float = 2.0,
        timeout: float = 300,
        max_concurrency: int = 2,
        max_429_retries: int = 5,
        retry_429_delay: float = 2.0,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.poll_interval = poll_interval
        self.timeout = timeout
        # 并发控制：同一时间最多运行 max_concurrency 个预测任务
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        # 429 并发限制兜底重试
        self.max_429_retries = max(0, int(max_429_retries))
        self.retry_429_delay = max(0.0, float(retry_429_delay))
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

    @staticmethod
    def _is_concurrency_limit(status: int, data) -> bool:
        """判断是否命中并发限制（HTTP 429 或 body 中的并发错误）。"""
        if status == 429:
            return True
        if isinstance(data, dict):
            if data.get("code") == 429:
                return True
            msg = str(data.get("message", "")).lower()
            if "concurrency" in msg or "simultaneous" in msg:
                return True
        return False

    async def submit(self, model: str, payload: dict) -> str:
        """提交生成任务，返回任务 id。模型名拼在 URL 中。

        遇 429 并发限制时指数退避重试（最多 max_429_retries 次）。
        """
        url = f"{self.base_url}/{model}"
        for attempt in range(self.max_429_retries + 1):
            try:
                async with self.session.post(
                    url, json=payload, headers=self._headers(), proxy=self.proxy
                ) as resp:
                    data = await resp.json()
            except aiohttp.ClientError as e:
                raise WavespeedError(f"提交请求失败: {e}") from e
            if resp.status == 200:
                task_id = data.get("data", {}).get("id")
                if not task_id:
                    raise WavespeedError(f"响应中未找到任务ID: {data}")
                return task_id
            if self._is_concurrency_limit(resp.status, data):
                if attempt < self.max_429_retries:
                    delay = self.retry_429_delay * (2 ** attempt)
                    logger.warning(
                        "[IMAGE] WaveSpeed 并发限制(429)，第 %d/%d 次重试，"
                        "等待 %.1fs 后重试",
                        attempt + 1, self.max_429_retries, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise WavespeedError(
                    f"提交失败 HTTP {resp.status}（并发限制重试 {self.max_429_retries} 次仍失败）: {data}"
                )
            raise WavespeedError(f"提交失败 HTTP {resp.status}: {data}")

    async def poll(
        self,
        task_id: str,
        on_status: Optional[Callable[[str], Awaitable[None] | None]] = None,
    ) -> list[str]:
        """轮询直到终态，返回 outputs（图片 URL 列表）。"""
        url = f"{self.base_url}/predictions/{task_id}/result"
        while True:
            try:
                async with self.session.get(
                    url, headers=self._headers(), proxy=self.proxy
                ) as resp:
                    data = await resp.json()
            except aiohttp.ClientError as e:
                raise WavespeedError(f"查询请求失败: {e}") from e
            if resp.status != 200:
                raise WavespeedError(f"查询失败 HTTP {resp.status}: {data}")
            task = data.get("data", data)
            status = task.get("status", "")
            if on_status:
                r = on_status(status)
                if asyncio.iscoroutine(r):
                    await r
            if status == "completed":
                outputs = task.get("outputs") or []
                return [o for o in outputs if isinstance(o, str) and o.strip()]
            if status in TERMINAL_FAILED:
                err = task.get("error") or task.get("message") or status
                raise WavespeedError(f"任务{status}: {err}")
            await asyncio.sleep(self.poll_interval)

    async def generate(self, model: str, payload: dict) -> list[str]:
        """提交并等待完成，返回图片 URL 列表。

        受信号量约束：同时运行的任务数不超过 max_concurrency，
        超出时排队等待（先到先得）。轮询受整体 timeout 约束。
        """
        async with self._semaphore:
            task_id = await self.submit(model, payload)
            try:
                return await asyncio.wait_for(
                    self.poll(task_id), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                raise WavespeedError(
                    f"任务轮询超时（{self.timeout}秒），任务ID: {task_id}"
                )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
