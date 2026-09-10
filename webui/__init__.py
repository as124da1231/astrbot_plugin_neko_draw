# -*- coding: utf-8 -*-
"""WebUI 后端 API 桥接。

向 AstrBot 注册 REST 端点：
- /astrbot_plugin_neko_draw/config        GET/POST  配置读写
- /astrbot_plugin_neko_draw/config/upload_file  POST  文件上传
- /astrbot_plugin_neko_draw/history       GET       历史列表（分页+筛选）
- /astrbot_plugin_neko_draw/history/<id>  GET/DELETE 单条详情/删除
- /astrbot_plugin_neko_draw/history/clear POST      清空历史
- /astrbot_plugin_neko_draw/history/stats GET       统计概览
- /astrbot_plugin_neko_draw/history/image/<id>/<idx> GET 历史图片
"""
from __future__ import annotations

import base64
import json
import time
import asyncio
from copy import deepcopy
from astrbot.api import logger
from ..core.uploads import store_upload
from ..core.configuration import validate_config
from ..core.providers import (
    fetch_provider_models, list_astrbot_providers, test_model_connection,
    test_provider_connection, infer_provider_type,
)
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astrbot.api.star import Context
    from astrbot.api.web import error_response, json_response, request
else:
    try:
        from astrbot.api.star import Context
        from astrbot.api.web import error_response, json_response, request
    except (ImportError, AttributeError):
        class Context:  # type: ignore[no-redef]
            pass
        def json_response(data=None, *, status_code=200, headers=None):
            return {"status_code": status_code, "data": data}
        def error_response(message="", *, status_code=400, data=None, headers=None):
            return {"status_code": status_code, "message": message, "data": data}
        request = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_neko_draw"


class WebUIBridge:
    def __init__(
        self,
        context: Context,
        config: Any,
        history_store: Any,
        apng_history_store: Any,
        data_dir: Path,
        plugin_dir: Path,
        on_config_saved: Any = None,
    ):
        self.context = context
        self.config = config
        self.history = history_store
        self.apng_history = apng_history_store
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.on_config_saved = on_config_saved

    # ------------------------------------------------------------------
    # 路由注册
    # ------------------------------------------------------------------
    def register_routes(self) -> None:
        routes = [
            (f"/{PLUGIN_NAME}/config", self.api_get_config, ["GET"], "Get plugin config and schema"),
            (f"/{PLUGIN_NAME}/config", self.api_save_config, ["POST"], "Save plugin config"),
            (f"/{PLUGIN_NAME}/config/upload_file", self.api_upload_file, ["POST"], "Upload config file"),
            (f"/{PLUGIN_NAME}/astrbot-providers", self.api_astrbot_providers, ["GET"], "List AstrBot providers"),
            (f"/{PLUGIN_NAME}/providers/test", self.api_test_provider, ["POST"], "Test provider connection"),
            (f"/{PLUGIN_NAME}/providers/models", self.api_provider_models, ["POST"], "Fetch provider models"),
            (f"/{PLUGIN_NAME}/models/test", self.api_test_model, ["POST"], "Test image model"),
            (f"/{PLUGIN_NAME}/history", self.api_list_history, ["GET"], "List generation history"),
            (f"/{PLUGIN_NAME}/history/stats", self.api_history_stats, ["GET"], "History statistics"),
            (f"/{PLUGIN_NAME}/history/clear", self.api_clear_history, ["POST"], "Clear all history"),
            (f"/{PLUGIN_NAME}/history/<record_id>", self.api_get_history, ["GET"], "Get history detail"),
            (f"/{PLUGIN_NAME}/history/delete", self.api_delete_history, ["POST"], "Delete history record"),
            (f"/{PLUGIN_NAME}/history/image/<record_id>/<idx>", self.api_history_image, ["GET"], "Serve history image"),
            (f"/{PLUGIN_NAME}/history/source/<record_id>/<idx>", self.api_history_source, ["GET"], "Serve edit source image"),
            (f"/{PLUGIN_NAME}/history/thumbnail/<record_id>/<kind>/<idx>", self.api_history_thumbnail, ["GET"], "Serve history thumbnail"),
            (f"/{PLUGIN_NAME}/apng-history", self.api_list_apng_history, ["GET"], "List APNG creations"),
            (f"/{PLUGIN_NAME}/apng-history/delete", self.api_delete_apng_history, ["POST"], "Delete APNG creation"),
            (f"/{PLUGIN_NAME}/apng-history/clear", self.api_clear_apng_history, ["POST"], "Clear APNG creations"),
            (f"/{PLUGIN_NAME}/apng-history/source/<record_id>/<idx>", self.api_apng_history_source, ["GET"], "Serve APNG source"),
            (f"/{PLUGIN_NAME}/apng-history/thumbnail/<record_id>/<idx>", self.api_apng_history_thumbnail, ["GET"], "Serve APNG thumbnail"),
        ]
        for path, handler, methods, desc in routes:
            try:
                self.context.register_web_api(path, handler, methods, desc)  # type: ignore[attr-defined]
            except Exception as e:
                print(f"[neko_draw] 注册路由失败 {path}: {e}")

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    async def api_get_config(self) -> Any:
        try:
            config_dict = {}
            if hasattr(self.config, "items"):
                config_dict = {str(k): v for k, v in self.config.items()}
            elif isinstance(self.config, dict):
                config_dict = dict(self.config)

            schema_file = self.plugin_dir / "_conf_schema.json"
            schema_dict = {}
            if schema_file.exists():
                schema_dict = json.loads(schema_file.read_text(encoding="utf-8"))

            return json_response({
                "status": "ok",
                "data": {"config": config_dict, "schema": schema_dict},
            })
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_save_config(self) -> Any:
        try:
            body = await request.json() if hasattr(request, "json") else {}
            new_config = body.get("config") if isinstance(body, dict) else None
            if not isinstance(new_config, dict):
                return error_response("缺少有效的 config 数据", status_code=400)

            schema = json.loads((self.plugin_dir / '_conf_schema.json').read_text(encoding='utf-8'))
            try:
                new_config = validate_config(new_config, schema)
            except ValueError as exc:
                return error_response(str(exc), status_code=400)
            old = deepcopy(dict(self.config))
            for k, v in new_config.items():
                self.config[k] = v

            persisted = False
            try:
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
                    persisted = True
                if callable(self.on_config_saved):
                    await self.on_config_saved()
            except Exception:
                self.config.clear()
                self.config.update(old)
                if persisted and hasattr(self.config, "save_config"):
                    try:
                        self.config.save_config()
                    except Exception as rollback_error:
                        logger.error(f"[neko_draw] 配置回滚写入失败: {rollback_error}")
                raise

            return json_response({
                "status": "ok",
                "message": "配置已保存",
                "data": dict(self.config) if hasattr(self.config, "items") else {},
            })
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_upload_file(self) -> Any:
        """上传文件（参考图/APNG首帧等），保存到 data_dir/files/。"""
        try:
            body = {}
            if hasattr(request, "json"):
                try:
                    parsed = await request.json()
                    if isinstance(parsed, dict):
                        body = parsed
                except Exception:
                    body = {}

            result = await asyncio.to_thread(store_upload, self.data_dir, body)
            return json_response({
                "status": "ok",
                "data": result,
            })
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_astrbot_providers(self) -> Any:
        """只返回提供商标识与模型信息，不把 AstrBot 密钥发送到浏览器。"""
        try:
            return json_response({
                "status": "ok",
                "data": {"items": list_astrbot_providers(self.context)},
            })
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_test_provider(self) -> Any:
        """测试草稿提供商配置；不提交任何生图任务。"""
        try:
            body = await request.json() if hasattr(request, "json") else {}
            item = body.get("provider") if isinstance(body, dict) else None
            if not isinstance(item, dict):
                return error_response("缺少有效的模型提供商配置", status_code=400)
            message = await test_provider_connection(item, self.context)
            return json_response({
                "status": "ok",
                "data": {"message": message},
            })
        except (ValueError, asyncio.TimeoutError) as e:
            message = "连接测试超时" if isinstance(e, asyncio.TimeoutError) else str(e)
            return error_response(message, status_code=400)
        except Exception as e:
            return error_response(f"连接测试失败：{type(e).__name__}", status_code=502)

    async def api_provider_models(self) -> Any:
        """测试连接并返回该连接公开的模型列表。"""
        try:
            body = await request.json() if hasattr(request, "json") else {}
            item = body.get("provider") if isinstance(body, dict) else None
            if not isinstance(item, dict):
                return error_response("缺少有效的模型提供商配置", status_code=400)
            models = await fetch_provider_models(item, self.context)
            detected = infer_provider_type(item.get("base_url"))
            message = (
                f"连接成功，发现 {len(models)} 个模型"
                if models or detected == "openai"
                else f"连接成功，{detected} 模型需要自定义添加"
            )
            return json_response({"status": "ok", "data": {
                "message": message,
                "models": models,
            }})
        except (ValueError, asyncio.TimeoutError) as e:
            return error_response("连接测试超时" if isinstance(e, asyncio.TimeoutError) else str(e), status_code=400)
        except Exception as e:
            return error_response(f"获取模型列表失败：{type(e).__name__}", status_code=502)

    async def api_test_model(self) -> Any:
        try:
            body = await request.json() if hasattr(request, "json") else {}
            provider = body.get("provider") if isinstance(body, dict) else None
            model = body.get("model") if isinstance(body, dict) else None
            if not isinstance(provider, dict) or not isinstance(model, dict):
                return error_response("缺少有效的提供商或模型配置", status_code=400)
            message = await test_model_connection(provider, model, self.context)
            return json_response({"status": "ok", "data": {"message": message}})
        except (ValueError, asyncio.TimeoutError) as e:
            return error_response("模型测试超时" if isinstance(e, asyncio.TimeoutError) else str(e), status_code=400)
        except Exception as e:
            return error_response(f"模型测试失败：{type(e).__name__}", status_code=502)

    # ------------------------------------------------------------------
    # 历史
    # ------------------------------------------------------------------
    async def api_list_history(self) -> Any:
        try:
            args = {}
            if hasattr(request, "query") and request.query:
                args = dict(request.query)
            elif hasattr(request, "args"):
                args = dict(request.args)

            page = int(args.get("page", 1))
            page_size = int(args.get("page_size", 20))
            user_id = args.get("user_id", "")
            model_template = args.get("model_template", "")
            status = args.get("status", "")
            start_time = float(args.get("start_time", 0))
            end_time = float(args.get("end_time", 0))

            result = self.history.list(
                page=page,
                page_size=page_size,
                user_id=user_id,
                model_template=model_template,
                status=status,
                start_time=start_time,
                end_time=end_time,
            )
            return json_response({"status": "ok", "data": result})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_get_history(self, record_id: str) -> Any:
        try:
            item = self.history.get(int(record_id))
            if item is None:
                return error_response("记录不存在", status_code=404)
            return json_response({"status": "ok", "data": item})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_delete_history(self) -> Any:
        try:
            body = await request.json() if hasattr(request, "json") else {}
            record_id = body.get("id") if isinstance(body, dict) else None
            if record_id is None:
                return error_response("缺少 id", status_code=400)
            ok = self.history.delete(int(record_id))
            if not ok:
                return error_response("记录不存在", status_code=404)
            return json_response({"status": "ok", "message": "已删除", "data": {"deleted": True}})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_clear_history(self) -> Any:
        try:
            count = self.history.clear()
            return json_response({"status": "ok", "data": {"deleted": count}})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_history_stats(self) -> Any:
        try:
            return json_response({"status": "ok", "data": self.history.stats()})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_history_image(self, record_id: str, idx: str) -> Any:
        """返回历史记录中的图片（二进制）。"""
        try:
            item = self.history.get(int(record_id))
            if item is None:
                return error_response("记录不存在", status_code=404)
            paths = item.get("image_paths", [])
            idx_int = int(idx)
            if idx_int < 0 or idx_int >= len(paths):
                return error_response("图片索引越界", status_code=404)
            img_path = Path(paths[idx_int])
            if not img_path.exists():
                return error_response("图片文件不存在", status_code=404)

            # 读取并返回 base64 data URI（AstrBot web API 可能不支持直接返回二进制）
            raw = img_path.read_bytes()
            ext = img_path.suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".gif": "image/gif",
            }.get(ext, "image/png")
            b64 = base64.b64encode(raw).decode("ascii")
            return json_response({
                "status": "ok",
                "data": {"mime": mime, "data_url": f"data:{mime};base64,{b64}"},
            })
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def _stored_image(self, path: Path) -> Any:
        if not path.is_file():
            return error_response("图片文件不存在", status_code=404)
        raw = await asyncio.to_thread(path.read_bytes)
        mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif"}.get(path.suffix.lower(), "image/png")
        b64 = base64.b64encode(raw).decode("ascii")
        return json_response({"status": "ok", "data": {
            "mime": mime, "data_url": f"data:{mime};base64,{b64}"}})

    async def api_history_source(self, record_id: str, idx: str) -> Any:
        try:
            item = self.history.get(int(record_id))
            if item is None:
                return error_response("记录不存在", status_code=404)
            paths = item.get("source_image_paths", [])
            index = int(idx)
            if index < 0 or index >= len(paths):
                return error_response("原图索引越界", status_code=404)
            return await self._stored_image(Path(paths[index]))
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_history_thumbnail(self, record_id: str, kind: str, idx: str) -> Any:
        try:
            item = self.history.get(int(record_id))
            if item is None:
                return error_response("记录不存在", status_code=404)
            key = "source_thumbnail_paths" if kind == "input" else "image_thumbnail_paths"
            paths = item.get(key, [])
            index = int(idx)
            if index < 0 or index >= len(paths) or not paths[index]:
                return error_response("缩略图不存在", status_code=404)
            return await self._stored_image(Path(paths[index]))
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_list_apng_history(self) -> Any:
        try:
            args = dict(request.query) if hasattr(request, "query") and request.query else {}
            result = self.apng_history.list(args.get("page", 1), args.get("page_size", 20))
            return json_response({"status": "ok", "data": result})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_delete_apng_history(self) -> Any:
        try:
            body = await request.json() if hasattr(request, "json") else {}
            if not isinstance(body, dict) or body.get("id") is None:
                return error_response("缺少 id", status_code=400)
            if not self.apng_history.delete(int(body["id"])):
                return error_response("记录不存在", status_code=404)
            return json_response({"status": "ok", "data": {"deleted": True}})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_clear_apng_history(self) -> Any:
        try:
            return json_response({"status": "ok", "data": {"deleted": self.apng_history.clear()}})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_apng_history_source(self, record_id: str, idx: str) -> Any:
        try:
            item = self.apng_history.get(int(record_id))
            if item is None:
                return error_response("记录不存在", status_code=404)
            paths = item.get("source_image_paths", [])
            index = int(idx)
            if index < 0 or index >= len(paths):
                return error_response("原图索引越界", status_code=404)
            return await self._stored_image(Path(paths[index]))
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_apng_history_thumbnail(self, record_id: str, idx: str) -> Any:
        try:
            item = self.apng_history.get(int(record_id))
            if item is None:
                return error_response("记录不存在", status_code=404)
            paths = item.get("source_thumbnail_paths", [])
            index = int(idx)
            if index < 0 or index >= len(paths) or not paths[index]:
                return error_response("缩略图不存在", status_code=404)
            return await self._stored_image(Path(paths[index]))
        except Exception as e:
            return error_response(str(e), status_code=500)
