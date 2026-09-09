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
from ..core.uploads import store_upload
from ..core.templates import derive_provider
from ..core.configuration import validate_config
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
        data_dir: Path,
        plugin_dir: Path,
    ):
        self.context = context
        self.config = config
        self.history = history_store
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)

    # ------------------------------------------------------------------
    # 路由注册
    # ------------------------------------------------------------------
    def register_routes(self) -> None:
        routes = [
            (f"/{PLUGIN_NAME}/config", self.api_get_config, ["GET"], "Get plugin config and schema"),
            (f"/{PLUGIN_NAME}/config", self.api_save_config, ["POST"], "Save plugin config"),
            (f"/{PLUGIN_NAME}/config/upload_file", self.api_upload_file, ["POST"], "Upload config file"),
            (f"/{PLUGIN_NAME}/history", self.api_list_history, ["GET"], "List generation history"),
            (f"/{PLUGIN_NAME}/history/stats", self.api_history_stats, ["GET"], "History statistics"),
            (f"/{PLUGIN_NAME}/history/clear", self.api_clear_history, ["POST"], "Clear all history"),
            (f"/{PLUGIN_NAME}/history/<record_id>", self.api_get_history, ["GET"], "Get history detail"),
            (f"/{PLUGIN_NAME}/history/delete", self.api_delete_history, ["POST"], "Delete history record"),
            (f"/{PLUGIN_NAME}/history/image/<record_id>/<idx>", self.api_history_image, ["GET"], "Serve history image"),
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

            if hasattr(self.config, "save_config"):
                try:
                    self.config.save_config()
                except Exception:
                    self.config.clear()
                    self.config.update(old)
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
