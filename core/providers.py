"""根据可添加的模型提供商配置创建 API 客户端。"""

import asyncio
import json
from typing import Any

import aiohttp

from .openapi import OpenAPIClient, OpenAPIError
from .runninghub import RunningHubClient
from .wavespeed import WavespeedClient


PROVIDER_TYPES = {"wavespeed", "runninghub", "openai", "astrbot"}


def normalize_provider_type(value: object) -> str:
    """把界面展示值规整为内部客户端类型。"""
    raw = str(value or "").strip().lower()
    return "openai" if raw in {"openai", "openapi"} else raw


def infer_provider_type(base_url: object) -> str:
    """仅根据地址识别调用协议；未知服务按通用 OpenAI 图像接口处理。"""
    value = str(base_url or "").strip().lower()
    if "wavespeed.ai" in value or "/api/v3" in value:
        return "wavespeed"
    if "runninghub.cn" in value or "/openapi/v2" in value:
        return "runninghub"
    return "openai"


def list_astrbot_providers(context: Any) -> list[dict[str, Any]]:
    """列出 AstrBot 已加载的聊天提供商，不向 WebUI 暴露密钥。"""
    result: list[dict[str, str]] = []
    getter = getattr(context, "get_all_providers", None)
    if not callable(getter):
        return result
    try:
        providers = getter() or []
    except Exception:
        return result
    for provider in providers:
        try:
            meta = provider.meta()
            provider_id = str(getattr(meta, "id", "") or "").strip()
            if not provider_id:
                continue
            config = getattr(provider, "provider_config", {}) or {}
            models: list[str] = []
            for value in (getattr(meta, "model", ""), config.get("model"), config.get("models")):
                if isinstance(value, str) and value.strip():
                    models.extend(part.strip() for part in value.split(",") if part.strip())
                elif isinstance(value, list):
                    models.extend(str(part).strip() for part in value if str(part).strip())
            result.append({
                "id": provider_id,
                "model": str(getattr(meta, "model", "") or ""),
                "models": list(dict.fromkeys(models)),
                "type": str(getattr(meta, "type", "") or ""),
            })
        except Exception:
            continue
    return sorted(result, key=lambda item: item["id"].lower())


def _normalize_astrbot_base_url(base_url: str, protocol: str) -> str:
    lower = base_url.lower()
    marker = "/api/v3" if protocol == "wavespeed" else "/openapi/v2"
    if protocol in {"wavespeed", "runninghub"} and marker in lower:
        return base_url[: lower.index(marker) + len(marker)]
    return base_url.rstrip("/")


def resolve_astrbot_provider(
    context: Any, provider_id: str, protocol: object = "auto"
) -> tuple[str, str, str, dict[str, str]]:
    getter = getattr(context, "get_provider_by_id", None)
    provider = getter(provider_id) if callable(getter) else None
    if provider is None:
        raise OpenAPIError(f"AstrBot 模型提供商「{provider_id}」不存在或未启用")
    config = getattr(provider, "provider_config", {}) or {}
    key_getter = getattr(provider, "get_current_key", None)
    api_key = str(key_getter() if callable(key_getter) else "")
    base_url = str(config.get("api_base") or "https://api.openai.com/v1").strip()
    selected = infer_provider_type(base_url)
    custom_headers = config.get("custom_headers")
    if not isinstance(custom_headers, dict):
        custom_headers = {}
    return selected, api_key, _normalize_astrbot_base_url(base_url, selected), custom_headers


class AstrBotProviderClient:
    """复用 AstrBot 提供商凭据，并按所选协议调用图像端点。"""

    def __init__(
        self, context: Any, provider_id: str, protocol: object = "auto",
        **client_options: Any,
    ):
        self.context = context
        self.provider_id = provider_id
        self.protocol = protocol
        self.client_options = client_options
        self.proxy = client_options.get("proxy")
        self._semaphore = asyncio.Semaphore(
            max(1, int(client_options.get("max_concurrency", 2)))
        )

    async def generate(self, model: str, payload: dict) -> list[str]:
        protocol, api_key, base_url, custom_headers = resolve_astrbot_provider(
            self.context, self.provider_id, self.protocol
        )
        common = {
            key: self.client_options[key]
            for key in ("proxy", "timeout") if key in self.client_options
        }
        if protocol == "wavespeed":
            client = WavespeedClient(
                api_key=api_key, base_url=base_url, max_concurrency=1,
                poll_interval=self.client_options.get("poll_interval", 2),
                max_429_retries=self.client_options.get("max_429_retries", 5),
                retry_429_delay=self.client_options.get("retry_429_delay", 2),
                **common,
            )
        elif protocol == "runninghub":
            client = RunningHubClient(
                api_key=api_key, base_url=base_url, max_concurrency=1,
                poll_interval=self.client_options.get("poll_interval", 2),
                **common,
            )
        else:
            client = OpenAPIClient(
                api_key=api_key, base_url=base_url, max_concurrency=1,
                custom_headers=custom_headers, **common,
            )
        async with self._semaphore:
            try:
                return await client.generate(model, dict(payload))
            finally:
                await client.close()

    async def close(self) -> None:
        return None


async def test_provider_connection(item: dict, context: Any, *, timeout: float = 20) -> str:
    """执行不会创建图片任务的连接测试。"""
    provider_type = infer_provider_type(item.get("base_url"))
    if str(item.get("source", "custom")).lower() == "astrbot" or normalize_provider_type(item.get("type")) == "astrbot":
        provider_id = str(item.get("astrbot_provider_id", "")).strip()
        protocol, api_key, base_url, _headers = resolve_astrbot_provider(context, provider_id)
        item = {"type": protocol, "api_key": api_key, "base_url": base_url}
        provider_type = protocol

    api_key = str(item.get("api_key", "")).strip()
    base_url = str(item.get("base_url", "")).strip().rstrip("/")
    if provider_type not in PROVIDER_TYPES - {"astrbot"}:
        raise ValueError("不支持的提供商类型")
    if not api_key:
        raise ValueError("请先填写 API Key")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("API Base URL 必须以 http:// 或 https:// 开头")

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=client_timeout) as session:
        if provider_type == "openai":
            async with session.get(f"{base_url}/models", headers=headers) as response:
                status = response.status
        elif provider_type == "wavespeed":
            async with session.get(base_url, headers=headers) as response:
                status = response.status
        else:
            async with session.post(
                f"{base_url}/query",
                headers=headers,
                json={"taskId": "neko-draw-connection-test"},
            ) as response:
                status = response.status
    if status in {401, 403}:
        raise ValueError(f"认证失败（HTTP {status}），请检查 API Key")
    if status >= 500:
        raise ValueError(f"服务暂时不可用（HTTP {status}）")
    if status >= 400:
        return f"服务器可达（HTTP {status}）；未产生生图任务，请再核对接口根地址和权限"
    return "连接与凭据验证成功，未产生生图任务"


async def fetch_provider_models(item: dict, context: Any, *, timeout: float = 20) -> list[str]:
    """验证提供商并读取 OpenAI 风格的模型列表。不会发起生图。"""
    provider_type = infer_provider_type(item.get("base_url"))
    if str(item.get("source", "custom")).lower() == "astrbot" or provider_type == "astrbot":
        provider_id = str(item.get("astrbot_provider_id", "")).strip()
        matching = next((p for p in list_astrbot_providers(context) if p["id"] == provider_id), None)
        if matching is None:
            raise ValueError("所选 AstrBot 提供商不存在或未启用")
        return list(matching.get("models") or [])
    api_key = str(item.get("api_key", "")).strip()
    base_url = str(item.get("base_url", "")).strip().rstrip("/")
    if not api_key:
        raise ValueError("请先填写 API Key")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError("API Base URL 必须以 http:// 或 https:// 开头")
    if provider_type in {"wavespeed", "runninghub"}:
        await test_provider_connection(item, context, timeout=timeout)
        return []
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(f"{base_url}/models", headers=headers) as response:
            raw = await response.text()
            if response.status in {401, 403}:
                raise ValueError(f"认证失败（HTTP {response.status}），请检查 API Key")
            if response.status >= 400:
                raise ValueError(f"获取模型列表失败（HTTP {response.status}）")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("服务已连接，但模型列表响应不是有效 JSON") from exc
    data = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
    models: list[str] = []
    for entry in data if isinstance(data, list) else []:
        model_id = entry.get("id") if isinstance(entry, dict) else entry
        if str(model_id or "").strip():
            models.append(str(model_id).strip())
    return list(dict.fromkeys(models))


async def test_model_connection(provider: dict, model: dict, context: Any, *, timeout: float = 20) -> str:
    """检查指定模型是否出现在提供商模型清单；不产生图片和费用。"""
    model_id = str(model.get("model") or model.get("id") or "").strip()
    if not model_id:
        raise ValueError("请先选择或填写模型")
    provider_type = infer_provider_type(provider.get("base_url"))
    if provider_type == "wavespeed":
        await test_provider_connection(provider, context, timeout=timeout)
        return f"WaveSpeed 连接正常；模型「{model_id}」将在请求时拼接到路径"
    models = await fetch_provider_models(provider, context, timeout=timeout)
    if models and model_id not in models:
        raise ValueError(f"连接正常，但模型列表中没有「{model_id}」")
    return f"模型「{model_id}」连接正常；本次未提交生图任务"


def _model_defaults(raw: dict) -> dict:
    mode = str(raw.get("mode", "text")).strip().lower()
    try:
        fallback_order = int(raw.get("fallback_order", 20))
    except (TypeError, ValueError):
        fallback_order = 20
    return {
        "__template_key": "model_template",
        "name": str(raw.get("name") or raw.get("model") or "").strip(),
        "model": str(raw.get("model") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "enabled_as_default": bool(raw.get("enabled_as_default", True)),
        "fallback_order": fallback_order,
        "refer_field": str(raw.get("refer_field") or ("images" if mode == "edit" else "")).strip(),
        "max_refer_images": int(raw.get("max_refer_images", 10 if mode == "edit" else 0) or 0),
        "min_prompt_length": int(raw.get("min_prompt_length", 0) or 0),
        "params": dict(raw.get("params") or {}),
    }


def sync_image_provider_config(config) -> None:
    """把新的一体化结构迁移/投影到既有稳定运行时，旧配置无需手工重填。"""
    integrated = config.get("image_providers")
    if not integrated and config.get("model_templates") and not config.get("_image_provider_config_migrated"):
        templates_by_provider: dict[str, list[dict]] = {}
        for template in config.get("model_templates", []) or []:
            if isinstance(template, dict):
                templates_by_provider.setdefault(str(template.get("provider", "")), []).append({
                    **{k: v for k, v in template.items() if k != "provider"},
                    "mode": "edit" if str(template.get("refer_field", "")).strip() else "text",
                })
        integrated = []
        for old in config.get("model_providers", []) or []:
            if not isinstance(old, dict):
                continue
            old_type = normalize_provider_type(old.get("type"))
            if old_type == "astrbot":
                provider_id = str(old.get("astrbot_provider_id", "")).strip()
                for template in templates_by_provider.get(str(old.get("name", "")), []):
                    alias = str(template.get("name", ""))
                    direct = f"@astrbot:{provider_id}:{template.get('model', '')}"
                    if alias == str(config.get("default_text_model", "")):
                        config["default_text_model_v2"] = direct
                    if alias == str(config.get("default_edit_model", "")):
                        config["default_edit_model_v2"] = direct
                continue
            integrated.append({
                "__template_key": "image_provider",
                "name": old.get("name", ""),
                "source": "astrbot" if old_type == "astrbot" else "custom",
                "api_key": old.get("api_key", ""),
                "base_url": old.get("base_url", ""),
                "astrbot_provider_id": old.get("astrbot_provider_id", ""),
                "enabled": True,
                "models": templates_by_provider.get(str(old.get("name", "")), []),
            })
        config["image_providers"] = integrated
        config["_image_provider_config_migrated"] = True
        if not config.get("default_text_model_v2"):
            config["default_text_model_v2"] = config.get("default_text_model", "")
        if not config.get("default_edit_model_v2"):
            config["default_edit_model_v2"] = config.get("default_edit_model", "")

    if not isinstance(config.get("image_providers"), list):
        return
    legacy_providers: list[dict] = []
    legacy_templates: list[dict] = []
    for provider in config.get("image_providers", []):
        if not isinstance(provider, dict) or not provider.get("enabled", True):
            continue
        name = str(provider.get("name", "")).strip()
        if not name:
            continue
        source = str(provider.get("source", "custom")).lower()
        protocol = {"wavespeed": "WaveSpeed", "runninghub": "RunningHub", "openai": "OpenAI"}[infer_provider_type(provider.get("base_url"))]
        legacy_providers.append({
            "__template_key": "provider_item", "name": name,
            "type": "AstrBot" if source == "astrbot" else protocol,
            "api_key": provider.get("api_key", ""), "base_url": provider.get("base_url", ""),
            "timeout": provider.get("timeout", config.get("timeout", 300)),
            "proxy": provider.get("proxy", ""),
            "custom_headers": provider.get("custom_headers", {}),
            "astrbot_provider_id": provider.get("astrbot_provider_id", ""),
            "astrbot_protocol": protocol if source == "astrbot" else "auto",
        })
        for model in provider.get("models", []) or []:
            if not isinstance(model, dict):
                continue
            item = _model_defaults(model)
            item["provider"] = name
            if item["name"] and item["model"]:
                legacy_templates.append(item)

    for key, mode in (("default_text_model_v2", "text"), ("default_edit_model_v2", "edit")):
        selected = str(config.get(key, "")).strip()
        if not selected.startswith("@astrbot:"):
            continue
        try:
            _, provider_id, model_id = selected.split(":", 2)
        except ValueError:
            continue
        provider_name = f"AstrBot · {provider_id}"
        if not any(p["name"] == provider_name for p in legacy_providers):
            legacy_providers.append({"__template_key": "provider_item", "name": provider_name,
                                     "type": "AstrBot", "astrbot_provider_id": provider_id,
                                     "astrbot_protocol": "auto"})
        virtual = _model_defaults({"name": selected, "model": model_id, "mode": mode,
                                   "enabled": True, "enabled_as_default": True})
        virtual["provider"] = provider_name
        legacy_templates.append(virtual)

    config["model_providers"] = legacy_providers
    config["model_templates"] = legacy_templates
    config["default_text_model"] = config.get("default_text_model_v2", config.get("default_text_model", ""))
    config["default_edit_model"] = config.get("default_edit_model_v2", config.get("default_edit_model", ""))


def ensure_provider_config(config) -> None:
    """把 v1.0.0 的固定密钥配置迁移为可添加的提供商列表。"""
    if config.get("model_providers") is None:
        has_legacy_credentials = any(str(config.get(key, "")).strip() for key in (
            "api_key", "runninghub_api_key", "openapi_api_key",
        ))
        config["model_providers"] = ([
            {
                "__template_key": "provider_item",
                "type": "WaveSpeed",
                "name": "WaveSpeed",
                "api_key": config.get("api_key", ""),
                "base_url": config.get("base_url", "https://api.wavespeed.ai/api/v3"),
            },
            {
                "__template_key": "provider_item",
                "type": "RunningHub",
                "name": "RunningHub",
                "api_key": config.get("runninghub_api_key", ""),
                "base_url": config.get("runninghub_base_url", "https://www.runninghub.cn/openapi/v2"),
            },
            {
                "__template_key": "provider_item",
                "type": "OpenAI",
                "name": "OpenAI",
                "api_key": config.get("openapi_api_key", ""),
                "base_url": config.get("openapi_base_url", "https://api.openai.com/v1"),
            },
        ] if has_legacy_credentials else [])

    names_by_type: dict[str, str] = {}
    provider_names: set[str] = set()
    for item in config.get("model_providers", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        provider_names.add(name)
        names_by_type.setdefault(normalize_provider_type(item.get("type")), name)

    for template in config.get("model_templates", []) or []:
        if not isinstance(template, dict):
            continue
        current = str(template.get("provider", "")).strip()
        if current in provider_names:
            continue
        migrated = names_by_type.get(normalize_provider_type(current))
        if migrated:
            template["provider"] = migrated
    sync_image_provider_config(config)


def create_providers(config, context: Any = None) -> dict[str, object]:
    """返回以用户定义的提供商名称为键的客户端字典。"""
    shared = {
        "proxy": config.get("proxy") or None,
        "timeout": float(config.get("timeout", 300)),
        "max_concurrency": int(config.get("max_concurrency", 2)),
    }
    polling = {"poll_interval": float(config.get("poll_interval", 2.0))}
    clients: dict[str, object] = {}

    for item in config.get("model_providers", []) or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        provider_type = "astrbot" if normalize_provider_type(item.get("type")) == "astrbot" else infer_provider_type(item.get("base_url"))
        if not name or provider_type not in PROVIDER_TYPES:
            continue
        item_shared = {
            **shared,
            "proxy": item.get("proxy") or shared["proxy"],
            "timeout": float(item.get("timeout", shared["timeout"]) or shared["timeout"]),
        }

        if provider_type == "astrbot":
            provider_id = str(item.get("astrbot_provider_id", "")).strip()
            if context is not None and provider_id:
                clients[name] = AstrBotProviderClient(
                    context=context,
                    provider_id=provider_id,
                    protocol=item.get("astrbot_protocol", "auto"),
                    poll_interval=polling["poll_interval"],
                    max_429_retries=int(config.get("max_429_retries", 5)),
                    retry_429_delay=float(config.get("retry_429_delay", 2.0)),
                    **item_shared,
                )
            continue

        api_key = str(item.get("api_key", ""))
        base_url = str(item.get("base_url", "")).strip()
        if provider_type == "wavespeed":
            clients[name] = WavespeedClient(
                api_key=api_key,
                base_url=base_url or "https://api.wavespeed.ai/api/v3",
                max_429_retries=int(config.get("max_429_retries", 5)),
                retry_429_delay=float(config.get("retry_429_delay", 2.0)),
                **item_shared,
                **polling,
            )
        elif provider_type == "runninghub":
            clients[name] = RunningHubClient(
                api_key=api_key,
                base_url=base_url or "https://www.runninghub.cn/openapi/v2",
                **item_shared,
                **polling,
            )
        else:
            clients[name] = OpenAPIClient(
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
                custom_headers=item.get("custom_headers") if isinstance(item.get("custom_headers"), dict) else None,
                **item_shared,
            )
    return clients
