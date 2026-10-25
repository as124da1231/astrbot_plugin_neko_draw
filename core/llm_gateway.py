# -*- coding: utf-8 -*-
"""LLM 能力：调用对话模型完成扩写 / 提取形象。

调用方只关心 ``(文本, 失败原因)`` 两元组，超时、无提供商、空结果、拒答
全部收敛成“空文本 + 原因”，由上层决定回退还是一句提示。
"""
from __future__ import annotations

import asyncio
import base64
import copy
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

from .text_utils import clean_model_text


def vision_prompt(template: str, image_paths: list) -> list:
    """把模板文本与本地图片组成 AstrBot 可序列化的多模态消息段。

    text_chat 的 prompt 只接受字符串或消息段列表；直接传 Image 组件会
    在 JSON 序列化时报错。这里统一转成 image_url + base64 data URL，
    与 OpenAI 兼容接口的多模态格式一致。
    """
    parts: list = [{"type": "text", "text": template}]
    for path in image_paths:
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            logger.warning("[neko_draw] 读取参考图失败，已跳过：%s（%s）", path, exc)
            continue
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        encoded = base64.b64encode(data).decode("ascii")
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return parts


def _looks_like_unsupported_kwarg(exc: TypeError, name: str) -> bool:
    """判断 TypeError 是否属于“这个参数不被接受”。

    兼容两种报错文案：
    - ``text_chat() got an unexpected keyword argument 'image_urls'``
    - ``text_chat() missing 1 required positional argument``（参数改名）
    """
    message = str(exc)
    return (
        name in message
        or "unexpected keyword argument" in message
        or "invalid keyword argument" in message
    )


class LlmGateway:
    """AstrBot 对话模型的薄封装，负责提供商解析与超时。"""

    def __init__(self, *, context: Any, umo_getter=None, conf=None):
        self.context = context
        self.conf = conf if conf is not None else {}
        self._umo_getter = umo_getter or (lambda event: str(
            getattr(event, "unified_msg_origin", "") or ""
        ))

    def umo(self, event: Any) -> str:
        try:
            return str(self._umo_getter(event) or "")
        except Exception:  # noqa: BLE001
            return ""

    def resolve_provider(self, event: Any, provider_id: str = ""):
        """按 ID 找提供商；找不到时回落到当前会话默认模型。"""
        provider = None
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:  # noqa: BLE001
                provider = None
            if provider is None:
                logger.warning(
                    "[neko_draw] 配置的提供商 %s 不存在（可能已被改名或删除），改用当前会话默认模型",
                    provider_id,
                )
        if provider is None:
            try:
                provider = self.context.get_using_provider(self.umo(event))
            except Exception:  # noqa: BLE001
                provider = None
        return provider

    @staticmethod
    def provider_model(provider: Any) -> str:
        getter = getattr(provider, "get_model", None)
        try:
            value = getter() if callable(getter) else ""
        except Exception:  # noqa: BLE001
            value = ""
        return str(value or getattr(provider, "model", "") or "")

    @staticmethod
    def provider_models(provider: Any) -> list[str]:
        """该提供商支持/配置的全部模型名（用于判断显式指定的模型是否可用）。"""
        models: list[str] = []
        meta_model = ""
        try:
            meta = provider.meta()
            meta_model = str(getattr(meta, "model", "") or "")
        except Exception:  # noqa: BLE001
            meta_model = ""
        config = getattr(provider, "provider_config", None)
        values: list[Any] = [meta_model]
        if isinstance(config, dict):
            values.extend([config.get("model"), config.get("models")])
        for value in values:
            if isinstance(value, str) and value.strip():
                models.extend(part.strip() for part in value.split(",") if part.strip())
            elif isinstance(value, list):
                models.extend(str(part).strip() for part in value if str(part).strip())
        default_model = LlmGateway.provider_model(provider)
        if default_model:
            models.append(default_model.strip())
        return list(dict.fromkeys(models))

    def _isolated_provider(self, provider: Any, enabled: bool):
        """按本次调用的开关复制提供商；不修改共享配置、客户端或聊天历史。"""
        if not enabled:
            return provider
        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return provider
        client = getattr(provider, "client", None)
        if not callable(getattr(client, "with_options", None)):
            raise ValueError("该提供商不支持独立会话，请在文字加工里关掉这条的独立会话")
        isolated = copy.copy(provider)
        isolated.provider_config = copy.deepcopy(config)
        extra = dict(isolated.provider_config.get("custom_extra_body") or {})
        extra["user"] = "neko-text-" + uuid.uuid4().hex
        isolated.provider_config["custom_extra_body"] = extra
        # SDK 副本共用 HTTP 连接池但拥有独立的请求选项；不能 close，否则会关闭共享池。
        isolated.client = client.with_options()
        return isolated

    async def chat(
        self,
        event: Any,
        *,
        template: str,
        system_prompt: str = "",
        timeout: int = 60,
        model: str = "",
        provider_id: str = "",
        image_paths: Optional[list] = None,
        meta: Optional[dict] = None,
        isolate: bool = False,
    ) -> tuple[str, str]:
        """调用对话模型，只返回文本。图片存在时一并交给模型（需支持视觉）。

        返回 ``(text, reason)``；失败时 text 为空字符串。
        ``meta`` 传入字典时，会把本次真正使用的提供商与模型写进去。
        ``isolate`` 为真时，本次请求附带一次性会话标识（避免反代把多次
        文字加工串成同一段对话）；为假时完全沿用原来的调用方式。
        """
        provider = self.resolve_provider(event, provider_id)
        if provider is None:
            return "", "未找到可用的对话模型"
        images = [str(p) for p in (image_paths or []) if str(p).strip()]
        # 显式指定的模型：属于该提供商就照用，明显不匹配才丢弃。
        # 以前只要指定了提供商就一律丢弃，导致界面上的「指定模型」永远不生效。
        use_model = ""
        if model:
            supported = self.provider_models(provider)
            if not supported or model in supported:
                use_model = model
            else:
                logger.info(
                    "[neko_draw] 预设模型 %s 不在提供商 %s 的模型列表中（%s），已改用提供商默认模型",
                    model, provider_id or "当前会话默认", "、".join(supported[:5]),
                )
        if meta is not None:
            try:
                meta["provider_id"] = str(
                    getattr(provider, "provider_id", "") or provider_id or ""
                )
                # 记录“本次真正使用的模型”：优先用显式指定的 use_model，
                # 没指定才回落到提供商默认。以前反着取，导致历史里显示的模型
                # 与真实请求不一致（用户会以为指定模型没生效）。
                meta["model"] = use_model or self.provider_model(provider)
            except Exception:  # noqa: BLE001
                logger.debug("[neko_draw] 读取提供商模型名失败", exc_info=True)
        call = {"system_prompt": system_prompt or None, "model": use_model or None}
        try:
            provider = self._isolated_provider(provider, isolate)
            if images:
                # 优先用 AstrBot 自带的 image_urls 参数（框架会自己做压缩、
                # 多模态格式转换和“非视觉模型自动降级”）；provider 恰好不认这个
                # 参数时再退回手拼消息段。
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(prompt=template, image_urls=list(images), **call),
                        timeout=timeout,
                    )
                except TypeError as exc:
                    # 只有“参数不被接受”才重试；其他 TypeError（真 bug）原样抛出，
                    # 交给外层统一记日志，避免白白等两次超时。
                    if not _looks_like_unsupported_kwarg(exc, "image_urls"):
                        raise
                    logger.info("[neko_draw] 提供商不接受 image_urls（%s），改用手拼消息段", exc)
                    response = await asyncio.wait_for(
                        provider.text_chat(prompt=vision_prompt(template, images), **call),
                        timeout=timeout,
                    )
            else:
                response = await asyncio.wait_for(
                    provider.text_chat(prompt=template, **call),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            logger.warning("[neko_draw] 指令模型超时（%ss）", timeout)
            return "", f"模型 {timeout} 秒内没有响应"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[neko_draw] 指令模型调用失败：%s", exc)
            # 原因要能指导排查：至少让用户看到是哪一类错误（鉴权/额度/网络等常见报错原文）
            return "", f"调用模型失败（{_short_error(exc)}）"
        text = clean_model_text(str(getattr(response, "completion_text", "") or ""))
        if not text:
            return "", "模型返回空结果"
        return text, ""


def _short_error(exc: Exception, limit: int = 120) -> str:
    """把异常压成一句短提示：太长时截断，避免把整段堆栈塞进聊天框。"""
    message = str(exc or "").strip() or type(exc).__name__
    message = " ".join(message.split())
    return message if len(message) <= limit else message[: limit - 1] + "…"
