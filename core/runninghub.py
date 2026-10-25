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
import base64
import binascii
import urllib.parse
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import aiohttp

from .key_rotation import (
    KeySwitchError,
    is_switchable_status,
    normalize_keys,
    run_with_keys,
)

logger = logging.getLogger("neko_draw")

TERMINAL_FAILED = ("FAILED",)

# 触发“换 Key 重试”的 RunningHub 错误码：余额不足 / Key 失效 / 并发频率超限。
# 工作流校验类（301/433/803）和会员到期类（435/436）换 Key 也没用，不在此列。
RH_KEY_SWITCH_CODES = {
    "416", "605", "812",   # 钱包余额不足
    "802", "806", "1002", "1014",  # Key 无效 / 未授权 / 用户不存在
    "421", "1003",         # 并发 / 频率超限
}


# RunningHub 实例类型：default 默认 / plus 48G 显存 / ultra 84G 显存。
# 界面上是下拉，但旧配置可能存了 "PLUS"、空格之类，提交前统一归一化。
INSTANCE_TYPES = ("default", "plus", "ultra")


def normalize_instance_type(value: object) -> str:
    """把实例类型归一化成 RunningHub 认识的值；无法识别时回落到 default。"""
    text = str(value or "").strip().lower()
    if text.endswith("g"):  # 允许填 "48g" / "84g" 这种显存写法
        text = {"48g": "plus", "84g": "ultra"}.get(text, text)
    return text if text in INSTANCE_TYPES else "default"


def _error_code(data: object) -> str:
    """取出 RunningHub 响应里的错误码（字符串），无则空串。"""
    if not isinstance(data, dict):
        return ""
    inner = _payload_of(data)
    code = (
        inner.get("errorCode") or inner.get("code")
        or data.get("errorCode") or data.get("code")
    )
    if code in (None, "", 0, "0"):
        return ""
    return str(code).strip()

# RunningHub 官方错误码中文说明（runninghub.cn/runninghub-api-doc-cn/doc-8287338）。
# 提交/轮询返回 errorCode 时，优先展示这里的中文提示，便于用户直接定位问题。
RH_ERROR_HINTS = {
    "301": "参数错误：必填参数缺失或类型不符，请核对调用参数。",
    "380": "工作流不存在：指定的工作流 ID 无效，请检查 RunningHub 工作流 ID 是否正确。",
    "412": "接口路径拼写错误：请检查调用的接口地址。",
    "415": "独占型 API 机器数不足（资源紧张）：请等待 30～120 秒后重试。",
    "416": "钱包余额不足：请前往 RunningHub 充值。",
    "421": "共享型 API 并发已达上限：请稍后重试或申请扩容。",
    "423": "未找到指定任务：任务 ID 错误或已被清理。",
    "433": "工作流校验未通过：节点参数或连线有误，请在 RunningHub 检查工作流。",
    "435": "未找到任务用户 API 实例：调用 48G 显存请传 instanceType=plus，84G 请传 ultra。",
    "436": "独占会员已到期：独占资源服务不可用。",
    "500": "RunningHub 服务端未知错误：请稍后重试或联系技术支持。",
    "605": "钱包余额不足：请前往 RunningHub 充值。",
    "801": "免费用户不支持 API Key：请升级账户等级。",
    "802": "API Key 未授权或已失效：请检查密钥是否正确。",
    "803": "nodeInfoList 不匹配：节点 ID 或字段名与工作流定义不一致（修改过工作流容易触发）。",
    "804": "任务正在运行中：请勿重复提交，耐心等待结果。",
    "805": "任务状态异常：任务可能已被中断或取消，重试即可。",
    "806": "未找到对应用户：API Key 关联的用户信息不存在。",
    "807": "未找到对应任务：查询不到该任务 ID 的记录。",
    "808": "文件上传失败：存储服务异常或网络中断，请重试。",
    "809": "文件大小超出限制：请压缩后重试。",
    "810": "工作流未保存或未运行：请在 RunningHub 保存并手动运行一次该工作流。",
    "811": "企业版 API Key 无效：密钥错误或无企业权限。",
    "812": "企业版余额不足：企业账户资金耗尽，请充值。",
    "813": "任务已排队：任务已受理，无需重复提交。",
    "901": "WebApp 不存在：关联的应用 ID 错误。",
    "1000": "未知错误：请重试或联系技术支持。",
    "1001": "请求链接无效：请检查调用链接。",
    "1002": "API Key 无效：请检查密钥。",
    "1003": "请求频率超限：请降低请求速度。",
    "1004": "任务不存在或已过期：请检查任务 ID。",
    "1005": "系统内部错误：请稍后重试。",
    "1006": "任务执行超时：请稍后重试。",
    "1007": "参数校验失败：请检查输入参数。",
    "1008": "文件大小超出限制：请压缩后重试。",
    "1009": "请求方法不支持：请确认使用 GET/POST。",
    "1010": "服务暂不可用：请稍后重试。",
    "1011": "系统繁忙：请求量较大，请稍后重试。",
    "1012": "上游服务响应异常：请稍后重试或联系技术支持。",
    "1013": "文件处理失败：请检查链接或重新上传。",
    "1014": "访问被拒绝：标准模型 API 仅限企业级共享 API Key 调用。",
    "1015": "生成失败：请重试。",
    "1101": "节点信息异常：工作流节点数据解析错误。",
    "1501": "内容审核未通过：请修改提示词或图片。",
    "1504": "模型响应超时：请稍后重试。",
    "1505": "禁止生成真人：请修改提示词或参考图。",
}


def runninghub_error_hint(code: object) -> str:
    """按 RunningHub 错误码返回中文说明；无对应说明时返回空串。"""
    if code is None:
        return ""
    return RH_ERROR_HINTS.get(str(code).strip(), "")


def _run_segment(run_mode: object) -> str:
    """返回 RunningHub 调用路径段：AI 应用用 ``ai-app``，其余走工作流 ``workflow``。"""
    mode = str(run_mode or "workflow").strip().lower().replace("-", "_")
    return "ai-app" if mode in ("ai_app", "app", "webapp") else "workflow"



def _payload_of(data: object) -> dict:
    """RunningHub 部分接口把结果包在 data 字段里；统一取出内层对象。"""
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and any(
            key in inner for key in ("taskId", "status", "results", "errorMessage")
        ):
            return inner
    return data if isinstance(data, dict) else {}


def _friendly_message(msg: object) -> str:
    """RunningHub 的报错常是“English | 中文”，优先保留中文部分。"""
    text = str(msg or "").strip()
    if "|" in text:
        parts = [p.strip() for p in text.split("|") if p.strip()]
        if parts:
            return parts[-1]
    return text


def _error_message(data: object) -> str:
    """从 RunningHub 响应里提取可读错误（兼容 errorCode/errorMessage 与 code/msg）。

    有官方错误码时优先输出内置的中文说明；否则回退到服务端返回的 errorMessage
    （“英文 | 中文”时取中文部分）。对外只给中文说明，不再附带错误码（避免重复）。
    """
    if not isinstance(data, dict):
        return ""
    inner = _payload_of(data)
    code = (
        inner.get("errorCode") or inner.get("code")
        or data.get("errorCode") or data.get("code")
    )
    msg = (
        inner.get("errorMessage") or inner.get("msg")
        or data.get("errorMessage") or data.get("msg")
    )
    has_code = code not in (None, "", 0, "0")
    text = runninghub_error_hint(code) if has_code else ""
    if not text:
        text = _friendly_message(msg)
    return text


class RunningHubError(Exception):
    pass


class RunningHubPending(Exception):
    """任务已提交但轮询超时：任务仍在 RunningHub 队列中，可稍后继续等待结果。

    与 ``RunningHubError`` 区分开：这不是失败，而是"还没跑完"。事件层收到后会把
    任务转入后台继续等待，出图后自动补发原图，并记录任务 ID 便于追溯。
    """

    def __init__(self, task_id: str, key: Optional[str] = None, message: str = ""):
        super().__init__(message or f"任务已提交，仍在排队（任务ID: {task_id}）")
        self.task_id = str(task_id)
        self.key = key


# 超时转后台后继续等待结果的上限（秒）：在此期间任务若完成就补发原图。
RESUME_WAIT_SECONDS = 1800


class RunningHubClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://www.runninghub.cn/openapi/v2",
        proxy: Optional[str] = None,
        poll_interval: float = 2.0,
        timeout: float = 300,
        max_concurrency: int = 2,
        api_keys: Optional[object] = None,
        data_dir: Optional[object] = None,
    ):
        self.api_keys = normalize_keys(api_key, api_keys)
        self.api_key = self.api_keys[0] if self.api_keys else ""
        self.base_url = base_url.rstrip("/")
        self.proxy = proxy
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.max_concurrency = max(1, int(max_concurrency))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._session: Optional[aiohttp.ClientSession] = None
        self.data_dir = Path(data_dir) if data_dir else None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
        return self._session

    def _headers(self, key: Optional[str] = None) -> dict:
        return {
            "Authorization": f"Bearer {key or self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _is_switchable(code: object = "", status: object = None) -> bool:
        """判断当前失败是否值得换 Key 重试。"""
        if status is not None and is_switchable_status(status):
            return True
        return str(code or "").strip() in RH_KEY_SWITCH_CODES

    @staticmethod
    def _report_task_id(callback: Optional[Callable[[str], None]], task_id: str) -> None:
        """把本次任务 ID 回报给调用方（用于历史记录）；回调异常不影响生成。"""
        if callback is None or not task_id:
            return
        try:
            callback(str(task_id))
        except Exception:  # noqa: BLE001
            logger.debug("任务 ID 回调执行失败", exc_info=True)

    def _upload_host(self) -> str:
        """从 base_url（如 https://www.runninghub.ai/openapi/v2）取出站点根地址。"""
        parts = urlsplit(self.base_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
        return "https://www.runninghub.cn"

    async def upload_reference(self, value: str, key: Optional[str] = None) -> str:
        """把 Base64 data URI 参考图上传到 RunningHub，返回可写入 LoadImage 的 fileName。

        RunningHub 工作流/AI 应用不接受直接塞 data URI 的图片字段，必须先上传拿到
        ``fileName``（形如 ``api/xxxx.png``）。非 data URI（公开 URL / 已有文件名）
        原样返回。上传接口：``POST {host}/task/openapi/upload``（multipart）。
        """
        match = re.match(
            r"^data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>.+)$",
            value or "", re.S,
        )
        if not match:
            return value
        mime = match.group("mime")
        try:
            content = base64.b64decode(match.group("data"))
        except (binascii.Error, ValueError):
            return value
        ext = mimetypes.guess_extension(mime) or ".png"
        if ext == ".jpe":
            ext = ".jpg"
        url = f"{self._upload_host()}/task/openapi/upload"
        form = aiohttp.FormData()
        form.add_field("apiKey", key or self.api_key)
        form.add_field("fileType", "input")
        form.add_field("file", content, filename=f"reference{ext}", content_type=mime)
        try:
            async with self.session.post(url, data=form, proxy=self.proxy) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise KeySwitchError(f"参考图上传失败: {e}") from e
        if resp.status != 200:
            if self._is_switchable(_error_code(data), resp.status):
                raise KeySwitchError(f"参考图上传失败 HTTP {resp.status}: {data}")
            raise RunningHubError(f"参考图上传失败 HTTP {resp.status}: {data}")
        inner = data.get("data") if isinstance(data.get("data"), dict) else data
        file_name = inner.get("fileName") if isinstance(inner, dict) else None
        if not file_name:
            detail = _error_message(data)
            message = detail or f"参考图上传失败：{data}"
            if self._is_switchable(_error_code(data), resp.status):
                raise KeySwitchError(message)
            raise RunningHubError(message)
        return str(file_name)

    async def submit(self, model: str, payload: dict, key: Optional[str] = None) -> str:
        """提交生成任务，返回 taskId。"""
        url = f"{self.base_url}/{model}"
        try:
            async with self.session.post(
                url, json=payload, headers=self._headers(key), proxy=self.proxy
            ) as resp:
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise KeySwitchError(f"提交请求失败: {e}") from e
        if resp.status != 200:
            message = f"提交失败 HTTP {resp.status}: {data}"
            if self._is_switchable(_error_code(data), resp.status):
                raise KeySwitchError(message)
            raise RunningHubError(message)
        inner = _payload_of(data)
        task_id = inner.get("taskId") or data.get("taskId")
        if not task_id:
            detail = _error_message(data)
            message = detail or f"响应中未找到 taskId: {data}"
            if self._is_switchable(_error_code(data), resp.status):
                raise KeySwitchError(message)
            raise RunningHubError(message)
        # 提交即成功（极少数情况），直接返回，poll 会处理
        return task_id

    async def poll(self, task_id: str, key: Optional[str] = None) -> list[str]:
        """轮询直到终态，返回 results 中的 url 列表。"""
        url = f"{self.base_url}/query"
        while True:
            try:
                async with self.session.post(
                    url, json={"taskId": task_id},
                    headers=self._headers(key), proxy=self.proxy,
                ) as resp:
                    data = await resp.json()
            except aiohttp.ClientError as e:
                raise RunningHubError(f"查询请求失败: {e}") from e
            if resp.status != 200:
                raise RunningHubError(f"查询失败 HTTP {resp.status}: {data}")
            inner = _payload_of(data)
            status = str(inner.get("status", "")).upper()
            if status == "SUCCESS":
                results = inner.get("results") or []
                urls = []
                for item in results:
                    if isinstance(item, dict):
                        u = item.get("url")
                        if u and isinstance(u, str) and u.strip():
                            urls.append(u)
                return urls
            if status in TERMINAL_FAILED:
                detail = _error_message(data)
                err = detail or inner.get("errorMessage") or inner.get("errorCode") or status
                raise RunningHubError(f"任务{status}: {err}")
            # QUEUED / RUNNING / 其他未知状态 → 继续等
            await asyncio.sleep(self.poll_interval)

    async def generate(
        self, model: str, payload: dict, *,
        on_task_id: Optional[Callable[[str], None]] = None,
    ) -> list[str]:
        """提交并等待完成，返回图片 URL 列表。受信号量约束。轮询受整体 timeout 约束。

        配了多个 Key 时按“主 Key → 备用 Key”顺序使用，遇到余额不足 / Key 失效 /
        限流 / 网络错误等可切换错误时自动换下一个 Key 重试。
        ``on_task_id`` 在提交成功后立刻回调本次任务 ID，便于写入历史/日志追溯。
        """
        payload = dict(payload or {})
        workflow = payload.pop("_comfyui", None)

        async def run(key: Optional[str]):
            if workflow:
                if str((workflow or {}).get("run_mode") or "").strip().lower() in (
                    "proxy", "native", "comfyui",
                ):
                    return await self._generate_proxy(payload, workflow, key)
                return await self._generate_workflow(
                    str(model), payload, workflow, key, on_task_id=on_task_id
                )
            async with self._semaphore:
                task_id = await self.submit(model, payload, key)
                logger.info("[IMAGE] RunningHub 任务已提交：任务ID=%s", task_id)
                self._report_task_id(on_task_id, task_id)
                try:
                    return await asyncio.wait_for(
                        self.poll(task_id, key), timeout=self.timeout
                    )
                except asyncio.TimeoutError:
                    raise RunningHubPending(
                        task_id, key,
                        f"任务轮询超时（{self.timeout}秒），任务ID: {task_id}",
                    ) from None

        try:
            return await run_with_keys(self.api_keys, run, logger, "RunningHub")
        except KeySwitchError as exc:
            raise RunningHubError(exc.message) from exc

    async def resume(self, task_id: str, key: Optional[str] = None) -> list[str]:
        """超时后继续等待任务结果（供后台补发使用）。

        与 ``generate`` 不同：不再占用并发信号量，并以更宽松的时间上限继续轮询，
        因此不会影响后续请求派发。任务最终完成则返回图片 URL 列表。
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            raise RunningHubError("缺少任务 ID，无法继续等待结果")
        limit = max(float(self.timeout), RESUME_WAIT_SECONDS)
        logger.info("[IMAGE] 继续等待超时任务：任务ID=%s，最长再等 %d 秒", task_id, int(limit))
        try:
            return await asyncio.wait_for(self.poll(task_id, key), timeout=limit)
        except asyncio.TimeoutError:
            raise RunningHubError(
                f"任务长时间未完成（已等待超过 {int(limit)} 秒）"
            ) from None

    async def fetch_result(self, task_id: str, key: Optional[str] = None) -> list[str]:
        """查询一次任务结果（不轮询），供「找回 / 补发」使用。

        已完成返回图片 URL 列表；仍在排队/运行、或该 Key 查不到该任务时返回空列表；
        任务明确失败时抛 RunningHubError。平台按 Key 隔离任务，必须用当初提交的 Key 查询。
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            raise RunningHubError("缺少任务 ID")
        url = f"{self.base_url}/query"
        try:
            async with self.session.post(
                url, json={"taskId": task_id},
                headers=self._headers(key), proxy=self.proxy,
            ) as resp:
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as e:
            raise RunningHubError(f"查询请求失败: {e}") from e
        if resp.status != 200:
            raise RunningHubError(f"查询失败 HTTP {resp.status}: {_error_message(data)}")
        inner = _payload_of(data)
        status = str(inner.get("status", "")).upper()
        if status in TERMINAL_FAILED:
            detail = _error_message(data) or inner.get("errorMessage") or status
            raise RunningHubError(f"任务{status}: {detail}")
        if status != "SUCCESS":
            return []
        results = inner.get("results") or []
        urls: list[str] = []
        for item in results:
            if isinstance(item, dict):
                u = item.get("url")
                if isinstance(u, str) and u.strip():
                    urls.append(u)
        return urls

    async def _generate_proxy(
        self, payload: dict, workflow: dict, key: Optional[str] = None,
    ) -> list[str]:
        """通道 C：把整份 API 格式 JSON 直接提交给 ComfyUI 原生代理。

        ``POST {host}/proxy/{KEY}/prompt`` → ``prompt_id`` →
        ``GET {host}/proxy/{KEY}/history/{prompt_id}`` →
        ``{host}/proxy/{KEY}/view?filename=&subfolder=&type=``。

        与工作流接口的区别：不需要平台侧保存工作流、可以带任意节点结构；
        代价是改动不落盘（下次不重传就没了），也不吃 instanceType / 个人队列。
        """
        from .comfyui_workflow import build_node_info_list

        graph = self._load_workflow_graph(workflow)
        node_list = build_node_info_list(
            workflow.get("nodes"), payload, workflow.get("refer_field", "")
        )
        if node_list:
            # 参考图（data URI）要先上传换成 fileName，和工作流接口一致
            for item in node_list:
                value = item.get("fieldValue")
                if isinstance(value, str) and value.startswith("data:"):
                    item["fieldValue"] = await self.upload_reference(value, key)
            missing = []
            for item in node_list:
                node = graph.get(str(item.get("nodeId")))
                if not isinstance(node, dict):
                    missing.append(str(item.get("nodeId")))
                    continue
                inputs = node.setdefault("inputs", {})
                if isinstance(inputs, dict):
                    inputs[str(item.get("fieldName"))] = item.get("fieldValue")
            if missing:
                raise RunningHubError(
                    "工作流 JSON 中找不到这些节点：" + "、".join(missing[:5])
                    + "，请重新上传与映射一致的工作流 JSON"
                )

        body = {
            "prompt": graph,
            "client_id": f"neko-draw-{abs(hash(str(graph)[:200])) % 10**8}",
        }
        url = f"{self._upload_host()}/proxy/{key or self.api_key}/prompt"
        async with self._semaphore:
            try:
                async with self.session.post(url, json=body, proxy=self.proxy) as resp:
                    data = await resp.json(content_type=None)
                    status = resp.status
            except aiohttp.ClientError as e:
                raise KeySwitchError(f"原生代理提交失败: {e}") from e
            if status != 200:
                detail = _error_message(data) or str(data)[:200]
                if self._is_switchable(_error_code(data), status):
                    raise KeySwitchError(f"原生代理提交失败（HTTP {status}）：{detail}")
                raise RunningHubError(f"原生代理提交失败（HTTP {status}）：{detail}")
            prompt_id = str((data or {}).get("prompt_id") or "").strip()
            if not prompt_id:
                node_errors = (data or {}).get("node_errors")
                detail = _error_message(data) or json.dumps(node_errors, ensure_ascii=False)[:300]
                raise RunningHubError(detail or "原生代理未返回 prompt_id，请检查工作流 JSON")
            logger.info("[IMAGE] RunningHub 原生代理任务已提交：prompt_id=%s", prompt_id)
            return await self._wait_proxy(prompt_id, key)

    async def _wait_proxy(self, prompt_id: str, key: Optional[str] = None) -> list[str]:
        """轮询原生代理的 history，直到出现结果或超时。"""
        host = self._upload_host()
        url = f"{host}/proxy/{key or self.api_key}/history/{prompt_id}"
        deadline = asyncio.get_running_loop().time() + max(60.0, float(self.timeout))
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                async with self.session.get(url, proxy=self.proxy) as resp:
                    data = await resp.json(content_type=None)
                    status = resp.status
            except aiohttp.ClientError as e:
                raise RunningHubError(f"原生代理查询失败: {e}") from e
            if status != 200:
                raise RunningHubError(f"原生代理查询失败（HTTP {status}）：{str(data)[:200]}")
            entry = data.get(prompt_id) if isinstance(data, dict) else None
            if isinstance(entry, dict):
                state = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                state_str = str(state.get("status_str") or "").lower()
                if state_str in ("error", "failed", "failure"):
                    messages = state.get("messages")
                    raise RunningHubError(
                        "原生代理执行失败：" + (str(messages)[:300] if messages else "工作流节点报错")
                    )
                urls = self._proxy_output_urls(entry, key)
                if urls:
                    return urls
                if state_str == "success":
                    raise RunningHubError("原生代理执行完成，但没有输出图片（检查是否有 SaveImage 节点）")
            if asyncio.get_running_loop().time() > deadline:
                raise RunningHubError(
                    f"原生代理任务超时（{int(self.timeout)} 秒未完成，prompt_id: {prompt_id}），请稍后重试"
                )

    def _proxy_output_urls(self, entry: dict, key: Optional[str]) -> list[str]:
        """把 history 里的 images 列表拼成可下载的 /view 地址。"""
        host = self._upload_host()
        urls: list[str] = []
        outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
        for output in outputs.values():
            if not isinstance(output, dict):
                continue
            for image in output.get("images") or []:
                if not isinstance(image, dict):
                    continue
                filename = str(image.get("filename") or "").strip()
                if not filename:
                    continue
                query = urllib.parse.urlencode({
                    "filename": filename,
                    "subfolder": str(image.get("subfolder") or ""),
                    "type": str(image.get("type") or "output"),
                })
                urls.append(f"{host}/proxy/{key or self.api_key}/view?{query}")
        return urls

    def _load_workflow_graph(self, workflow: dict) -> dict:
        """读出该条目保存的整份 API JSON（只留最新一份）。"""
        name = str((workflow or {}).get("raw_json") or "").strip()
        if not name:
            raise RunningHubError(
                "该条目没有保存工作流 JSON：请到「ComfyUI 工作流」里重新上传一次 API 格式 JSON"
            )
        if self.data_dir is None:
            raise RunningHubError("缺少数据目录，无法读取工作流 JSON")
        path = (Path(self.data_dir) / "neko_draw_workflows" / Path(name).name)
        if not path.is_file():
            raise RunningHubError("工作流 JSON 文件已丢失，请重新上传一次")
        try:
            graph = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            raise RunningHubError(f"工作流 JSON 读取失败：{e}") from e
        if not isinstance(graph, dict):
            raise RunningHubError("工作流 JSON 结构不正确，应为 API 格式")
        return graph

    async def _generate_workflow(
        self, workflow_id: str, payload: dict, workflow: dict,
        key: Optional[str] = None, *,
        on_task_id: Optional[Callable[[str], None]] = None,
    ) -> list[str]:
        """按 ComfyUI 工作流提交：model 即 workflowId，mapping 由 payload['_comfyui'] 承载。"""
        from .comfyui_workflow import build_node_info_list

        if not str(workflow_id).strip():
            raise RunningHubError("未配置 RunningHub 工作流 ID / AI 应用 ID")
        node_list = build_node_info_list(workflow.get("nodes"), payload, workflow.get("refer_field", ""))
        if not node_list:
            raise RunningHubError(
                "工作流没有可用的参数映射：请先在插件面板上传 API 工作流 JSON 并确认参数"
            )
        # 参考图（data URI）需先上传到 RunningHub，拿到 fileName 才能写入 LoadImage 节点。
        for item in node_list:
            if isinstance(item.get("fieldValue"), str) and item["fieldValue"].startswith("data:"):
                item["fieldValue"] = await self.upload_reference(item["fieldValue"], key)
        body = {
            "addMetadata": True,
            "nodeInfoList": node_list,
            "instanceType": normalize_instance_type(workflow.get("instance_type")),
            "usePersonalQueue": bool(workflow.get("use_personal_queue", False)),
        }
        async with self._semaphore:
            task_id = await self._submit_workflow(workflow_id, body, workflow.get("run_mode"), key)
            logger.info("[IMAGE] RunningHub 任务已提交：任务ID=%s", task_id)
            self._report_task_id(on_task_id, task_id)
            try:
                return await asyncio.wait_for(self.poll(task_id, key), timeout=self.timeout)
            except asyncio.TimeoutError:
                raise RunningHubPending(
                    task_id, key,
                    f"任务轮询超时（{self.timeout}秒），任务ID: {task_id}",
                ) from None

    async def _submit_workflow(
        self, workflow_id: str, body: dict, run_mode: object = None,
        key: Optional[str] = None,
    ) -> str:
        """提交 ComfyUI 任务，返回 taskId。

        run_mode 为 ``ai_app`` 时走 RunningHub「AI 应用」接口
        ``/run/ai-app/{appId}``（ID 取自 /ai-detail/ 页面）；否则走工作流接口
        ``/run/workflow/{workflowId}``。两者请求体一致，节点参数用应用/工作流导出的
        nodeInfoList 即可。
        """
        segment = _run_segment(run_mode)
        url = f"{self.base_url}/run/{segment}/{workflow_id}"
        try:
            async with self.session.post(
                url, json=body, headers=self._headers(key), proxy=self.proxy
            ) as resp:
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise KeySwitchError(f"提交请求失败: {e}") from e
        if resp.status != 200:
            detail = _error_message(data)
            label = "AI 应用" if segment == "ai-app" else "工作流"
            message = f"{label}提交失败（HTTP {resp.status}）" + (f"：{detail}" if detail else f": {data}")
            if self._is_switchable(_error_code(data), resp.status):
                raise KeySwitchError(message)
            raise RunningHubError(message)
        inner = _payload_of(data)
        task_id = inner.get("taskId") or data.get("taskId")
        if not task_id:
            detail = _error_message(data)
            message = detail or f"响应中未找到 taskId: {data}"
            if self._is_switchable(_error_code(data), resp.status):
                raise KeySwitchError(message)
            raise RunningHubError(message)
        return task_id


    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
