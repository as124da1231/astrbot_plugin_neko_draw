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
from ..core.uploads import store_upload, store_upload_bytes, MAX_UPLOAD_BYTES
from ..core.comfyui_workflow import analyze_workflow, merge_mapping, role_meta
from ..core.configuration import validate_config
from ..core.providers import (
    fetch_provider_balances, fetch_provider_models, list_astrbot_providers,
    test_model_connection, test_provider_connection, infer_provider_type,
    test_comfyui_workflow,
)
from pathlib import Path
from typing import TYPE_CHECKING, Any

_VERSION_CACHE: dict[str, str] = {}


def _plugin_version() -> str:
    """读 metadata.yaml 里的版本号（只读一次），读不到返回空串。"""
    if "value" in _VERSION_CACHE:
        return _VERSION_CACHE["value"]
    version = ""
    try:
        import re
        text = (Path(__file__).resolve().parent.parent / "metadata.yaml").read_text(encoding="utf-8")
        found = re.search(r"^version:\s*(\S+)", text, re.M)
        version = found.group(1).strip() if found else ""
    except Exception:  # noqa: BLE001
        version = ""
    _VERSION_CACHE["value"] = version
    return version


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
        recall_handler: Any = None,
        command_stores: Any = None,
        llm_history: Any = None,
    ):
        self.context = context
        self.config = config
        self.history = history_store
        self.apng_history = apng_history_store
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.on_config_saved = on_config_saved
        self.recall_handler = recall_handler
        self.command_stores = command_stores
        self.llm_history = llm_history

    # ------------------------------------------------------------------
    # 路由注册
    # ------------------------------------------------------------------
    def register_routes(self) -> None:
        routes = [
            (f"/{PLUGIN_NAME}/config", self.api_get_config, ["GET"], "Get plugin config and schema"),
            (f"/{PLUGIN_NAME}/config", self.api_save_config, ["POST"], "Save plugin config"),
            (f"/{PLUGIN_NAME}/config/upload_file", self.api_upload_file, ["POST"], "Upload config file"),
            (f"/{PLUGIN_NAME}/config/upload/reference_image", self._image_upload_handler("reference_image"), ["POST"], "Upload reference image"),
            (f"/{PLUGIN_NAME}/config/upload/drawing_first_frame", self._image_upload_handler("drawing_first_frame_path"), ["POST"], "Upload drawing first frame"),
            (f"/{PLUGIN_NAME}/config/upload/apng_first_frame", self._image_upload_handler("apng_first_frame_path"), ["POST"], "Upload APNG first frame"),
            (f"/{PLUGIN_NAME}/comfyui/parse-workflow", self.api_parse_workflow, ["POST"], "Parse ComfyUI workflow JSON"),
            (f"/{PLUGIN_NAME}/comfyui/merge-mapping", self.api_merge_mapping, ["POST"], "Merge workflow field mapping"),
            (f"/{PLUGIN_NAME}/comfyui/download-workflow", self.api_download_workflow, ["GET"], "Download current workflow JSON"),
            (f"/{PLUGIN_NAME}/comfyui/test-workflow", self.api_test_comfyui_workflow, ["POST"], "Test-run a ComfyUI workflow"),
            (f"/{PLUGIN_NAME}/preset/param-meta", self.api_preset_param_meta, ["GET"], "Preset parameter metadata"),
            (f"/{PLUGIN_NAME}/commands/presets", self.api_command_presets, ["GET"], "List expand/extract command presets"),
            (f"/{PLUGIN_NAME}/commands/save", self.api_save_command_presets, ["POST"], "Save expand/extract command presets"),
            (f"/{PLUGIN_NAME}/llm-history", self.api_list_llm_history, ["GET"], "List expand/extract LLM calls"),
            (f"/{PLUGIN_NAME}/llm-history/stats", self.api_llm_history_stats, ["GET"], "LLM call statistics"),
            (f"/{PLUGIN_NAME}/llm-history/clear", self.api_clear_llm_history, ["POST"], "Clear LLM call history"),
            (f"/{PLUGIN_NAME}/llm-history/delete", self.api_delete_llm_history, ["POST"], "Delete one LLM call record"),
            (f"/{PLUGIN_NAME}/llm-history/<record_id>", self.api_get_llm_history, ["GET"], "Get LLM call detail"),
            (f"/{PLUGIN_NAME}/astrbot-providers", self.api_astrbot_providers, ["GET"], "List AstrBot providers"),
            (f"/{PLUGIN_NAME}/providers/test", self.api_test_provider, ["POST"], "Test provider connection"),
            (f"/{PLUGIN_NAME}/providers/models", self.api_provider_models, ["POST"], "Fetch provider models"),
            (f"/{PLUGIN_NAME}/providers/balance", self.api_provider_balances, ["POST"], "Query provider key balances"),
            (f"/{PLUGIN_NAME}/models/test", self.api_test_model, ["POST"], "Test image model"),
            (f"/{PLUGIN_NAME}/history", self.api_list_history, ["GET"], "List generation history"),
            (f"/{PLUGIN_NAME}/history/stats", self.api_history_stats, ["GET"], "History statistics"),
            (f"/{PLUGIN_NAME}/history/clear", self.api_clear_history, ["POST"], "Clear all history"),
            (f"/{PLUGIN_NAME}/history/<record_id>", self.api_get_history, ["GET"], "Get history detail"),
            (f"/{PLUGIN_NAME}/history/delete", self.api_delete_history, ["POST"], "Delete history record"),
            (f"/{PLUGIN_NAME}/history/recall", self.api_recall_history, ["POST"], "Recall generation image from provider"),
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
    @staticmethod
    def _plugin_version() -> str:
        return _plugin_version()

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
                "data": {"config": config_dict, "schema": schema_dict, "version": self._plugin_version()},
            })
        except Exception as e:
            return error_response(str(e), status_code=500)

    def _data_json_backup(self) -> dict:
        """保存前把 plugin_data 下的 JSON 快照读进内存。

        回滚时运行组件会按“旧配置”重建，而各存储在重建过程中会**就地重写**这些
        本地 JSON（预设、提示词、白名单）。若不在回滚后还原，刚被拒绝的那次保存
        会连带把用户通过命令新增、尚未进入配置的数据抹掉。
        """
        backup: dict[str, bytes | None] = {}
        folder = Path(self.data_dir)
        try:
            for path in folder.glob("*.json"):
                try:
                    backup[path.name] = path.read_bytes()
                except OSError:
                    backup[path.name] = None
        except OSError:
            return {}
        return backup

    def _restore_data_json(self, backup: dict) -> None:
        if not backup:
            return
        folder = Path(self.data_dir)
        for name, content in backup.items():
            try:
                if content is None:
                    continue
                (folder / name).write_bytes(content)
            except OSError:
                logger.warning("[neko_draw] 回滚本地数据失败：%s", name, exc_info=True)

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
            except TypeError as exc:
                # 字段类型完全传错时 int()/float() 会抛 TypeError，这仍然属于
                # “用户数据不合法”，应当返回 400 和可读原因，而不是 500 内部错误。
                return error_response(f"配置格式不正确：{exc}", status_code=400)
            old = deepcopy(dict(self.config))
            # 防误删：列表类配置为空、而磁盘上已有内容时，拒绝用空值覆盖。
            # 典型场景是网页还没加载完就点了保存，会把全部指令/预设清空。
            # 用户确实删光了某个分区时，前端会通过 allowEmpty 显式声明，予以放行。
            allow_empty = set()
            raw_allow = body.get("allowEmpty") if isinstance(body, dict) else None
            if isinstance(raw_allow, list):
                allow_empty = {str(x) for x in raw_allow}
            for key, spec in schema.items():
                if spec.get("type") not in ("template_list", "list") or key in allow_empty:
                    continue
                submitted = new_config.get(key)
                existing = old.get(key)
                if submitted == [] and isinstance(existing, list) and existing:
                    return error_response(
                        f"「{spec.get('description') or key}」提交为空，但服务器上已有 {len(existing)} 条内容，已拒绝覆盖；请刷新页面后重试",
                        status_code=400,
                    )

            # 「基线对比」：前端把它加载页面时看到的触发词清单一并发上来。
            # 只有“这份页面见过、现在没了”才算用户主动删除；服务器上存在、但这份页面
            # 从没见过（另一个标签页 / 部署前就打开的旧草稿）的条目一律原样保留，
            # 否则过期草稿一点保存就会把别人的数据整片删掉。
            base_presets = body.get("basePresets") if isinstance(body, dict) else None
            section_keys = {"expand": "expand_presets", "extract": "extract_presets"}
            deleted_by_user: dict[str, set] = {}
            if isinstance(base_presets, dict):
                for section, key in section_keys.items():
                    submitted = new_config.get(key)
                    base = base_presets.get(section)
                    if not isinstance(submitted, list) or not isinstance(base, list):
                        continue
                    base_triggers = {str(t).strip() for t in base if str(t).strip()}
                    submitted_triggers = {
                        str(item.get("trigger") or "").strip()
                        for item in submitted if isinstance(item, dict)
                    }
                    submitted_triggers.discard("")
                    preserved = [
                        item for item in (old.get(key) or [])
                        if isinstance(item, dict)
                        and str(item.get("trigger") or "").strip() not in base_triggers
                        and str(item.get("trigger") or "").strip() not in submitted_triggers
                    ]
                    if preserved:
                        new_config[key] = list(submitted) + preserved
                        logger.info(
                            "[neko_draw] 页面基线里没有的 %d 条指令已保留（避免过期草稿误删）",
                            len(preserved),
                        )
                    deleted_by_user[section] = base_triggers - submitted_triggers

            for k, v in new_config.items():
                self.config[k] = v

            # 先备份 plugin_data 的 JSON（含删除黑名单），再动任何本地文件：
            # 回滚时要把它们一起还原，否则“保存失败”也会把删除标记留下来。
            data_backup = self._data_json_backup()

            # 以提交的完整列表为准记一次删除账：
            # 本地还留着、但这次没提交的触发词记为已删除（否则并集合并会把它并回来，
            # 表现就是“点了删除、保存后还在”）；重新提交的触发词则解除删除标记。
            try:
                from ..core.command_presets import sync_removed_blacklist
                for section in ("expand", "extract"):
                    submitted = new_config.get(f"{section}_presets")
                    if not isinstance(submitted, list):
                        # 没提交这个分区就不能判断删除意图，一律不动
                        continue
                    triggers = [
                        str(item.get("trigger") or "").strip()
                        for item in submitted if isinstance(item, dict)
                    ]
                    sync_removed_blacklist(
                        self.data_dir, section, triggers,
                        deleted=deleted_by_user.get(section),
                    )
            except Exception:  # noqa: BLE001
                logger.debug("[neko_draw] 同步指令删除标记失败", exc_info=True)

            # 在重建运行组件之前取快照：重载会把 comfyui 工作流投影成派生模板等运行时
            # 结构，那些不属于用户配置，回灌给前端会被当成用户数据再提交一次。
            saved_snapshot = dict(self.config) if hasattr(self.config, "items") else {}

            # 顺序很重要：先让新配置在内存里跑通（重建运行组件、连提供商），
            # 全部成功后才写盘。中途失败只把内存恢复成旧值，绝不往磁盘写空配置。
            try:
                if callable(self.on_config_saved):
                    await self.on_config_saved()
                if hasattr(self.config, "save_config"):
                    self.config.save_config()
            except Exception as rollback_error:
                logger.warning("[neko_draw] 配置未生效，已恢复原设置：%s", rollback_error)
                self.config.clear()
                self.config.update(old)
                try:
                    if callable(self.on_config_saved):
                        await self.on_config_saved()
                except Exception:  # noqa: BLE001
                    logger.error("[neko_draw] 恢复原配置时运行组件重建失败", exc_info=True)
                self._restore_data_json(data_backup)
                raise

            # 返回“用户提交并已写入”的配置快照（重载前取的），而不是重载后被投影过的
            # 内存状态；否则前端会把派生结构当成用户配置再提交回来。
            return json_response({
                "status": "ok",
                "message": "配置已保存",
                "data": saved_snapshot,
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
    # multipart 上传（拖拽/粘贴/点击选择）与 ComfyUI 工作流解析
    # ------------------------------------------------------------------
    async def _read_multipart_file(self, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
        """从 AstrBot 请求中读取 multipart 字段 file 的字节（分块限流读取）。"""
        if request is None or not hasattr(request, "files"):
            raise ValueError("当前环境不支持 multipart 文件上传")
        files = await request.files()
        upload = files.get("file") if hasattr(files, "get") else None
        if upload is None:
            raise ValueError("未收到上传文件（字段名应为 file）")
        data = bytearray()
        try:
            while True:
                chunk = await upload.read(min(1024 * 1024, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_bytes:
                    # 说明真实上限：工作流 JSON 是 5MB，图片是 20MB，不能一律写 20MB
                    limit_mb = max(1, max_bytes // (1024 * 1024))
                    raise ValueError(f"文件超过 {limit_mb} MB 限制")
        finally:
            closer = getattr(upload, "close", None)
            if callable(closer):
                try:
                    await closer()
                except Exception:
                    pass
        return bytes(data)

    def _image_upload_handler(self, config_key: str):
        """生成绑定到某个配置字段的图片上传处理器。"""
        async def handler() -> Any:
            try:
                raw = await self._read_multipart_file()
                result = await asyncio.to_thread(
                    store_upload_bytes, self.data_dir, config_key, raw
                )
                return json_response({"status": "ok", "data": result})
            except ValueError as e:
                return error_response(str(e), status_code=400)
            except Exception as e:
                return error_response(str(e), status_code=500)
        return handler

    async def api_parse_workflow(self) -> Any:
        """解析上传的 ComfyUI API 工作流 JSON，返回可设置的字段清单。

        顺手把原始 JSON 存到 ``plugin_data/neko_draw_workflows/<md5>.json``（只留最新
        一份），条目里只记文件名。这样「下载当前工作流」和「原生代理直接提交整份
        JSON」都能用同一份文件，不必让用户去平台再导一次。
        """
        try:
            raw = await self._read_multipart_file(max_bytes=5 * 1024 * 1024)
            try:
                workflow = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                return error_response(f"工作流 JSON 解析失败：{e}", status_code=400)
            nodes = await asyncio.to_thread(analyze_workflow, workflow)
            if not nodes:
                return error_response(
                    "这份 JSON 里没有可识别的参数：请确认上传的是 ComfyUI「导出（API 格式）」"
                    "得到的文件，而不是画布工程文件",
                    status_code=400,
                )
            stored = await asyncio.to_thread(self._store_workflow_json, raw)
            return json_response({
                "status": "ok",
                "data": {"nodes": nodes, "total": len(nodes), "raw_json": stored},
            })
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(str(e), status_code=500)

    def _store_workflow_json(self, raw: bytes) -> str:
        """按内容哈希保存原始工作流 JSON，返回文件名（同内容重复上传只占一份）。"""
        import hashlib

        digest = hashlib.md5(raw).hexdigest()
        name = f"{digest}.json"
        folder = Path(self.data_dir) / "neko_draw_workflows"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        # 空文件说明上次写入被中断（磁盘满 / 进程被杀），必须重写，
        # 否则同一份 JSON 重传时会被“文件已存在”挡掉，永远无法自愈。
        if not target.is_file() or target.stat().st_size == 0:
            target.write_bytes(raw)
        return name

    async def api_merge_mapping(self) -> Any:
        """重新上传工作流时，把旧映射表里用户调过的用途搬到新字段上。"""
        try:
            body = await request.json() if hasattr(request, "json") else {}
            if not isinstance(body, dict):
                return error_response("请求体格式不正确", status_code=400)
            old_nodes = body.get("old") if isinstance(body.get("old"), list) else []
            new_nodes = body.get("new") if isinstance(body.get("new"), list) else []
            result = await asyncio.to_thread(merge_mapping, old_nodes, new_nodes)
            return json_response({"status": "ok", "data": result})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_download_workflow(self) -> Any:
        """下载某个工作流条目当前使用的 API 格式 JSON。"""
        try:
            args = {}
            if hasattr(request, "query") and request.query:
                args = dict(request.query)
            elif hasattr(request, "args"):
                args = dict(request.args)
            name = str(args.get("name") or "").strip()
            if not name:
                return error_response("缺少工作流名称", status_code=400)
            items = self.config.get("comfyui_workflows") or []
            item = next(
                (one for one in items if isinstance(one, dict)
                 and str(one.get("name") or "").strip() == name),
                None,
            )
            if item is None:
                return error_response("没有找到这个工作流条目", status_code=404)
            raw_name = str(item.get("raw_json") or "").strip()
            if not raw_name:
                return error_response(
                    "这个条目还没有保存工作流 JSON，重新上传一次 API 格式 JSON 即可",
                    status_code=404,
                )
            path = Path(self.data_dir) / "neko_draw_workflows" / Path(raw_name).name
            if not path.is_file():
                return error_response("工作流 JSON 文件已丢失，请重新上传一次", status_code=404)
            text = await asyncio.to_thread(path.read_text, "utf-8")
            return json_response({
                "status": "ok",
                "data": {
                    "filename": f"{name}.json",
                    "text": text,
                    "raw_json": Path(raw_name).name,
                },
            })
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_test_comfyui_workflow(self) -> Any:
        """真实试跑一次 ComfyUI 工作流，验证 ID / 参数映射 / 节点是否可用（会生成一张）。"""
        try:
            body = await request.json() if hasattr(request, "json") else {}
            workflow = body.get("workflow") if isinstance(body, dict) else None
            provider_name = str(body.get("provider") or "").strip() if isinstance(body, dict) else ""
            result = await test_comfyui_workflow(workflow, provider_name, self.config)
            return json_response({"status": "ok", "data": result})
        except (ValueError, asyncio.TimeoutError) as e:
            message = "测试超时，请稍后重试" if isinstance(e, asyncio.TimeoutError) else str(e)
            return error_response(message, status_code=400)
        except Exception as e:
            return error_response(f"测试失败：{type(e).__name__}", status_code=502)

    async def api_preset_param_meta(self) -> Any:
        """返回预设可选参数（用途键）的中文标签与控件类型。只读，不触发任何生成。"""
        try:
            return json_response({
                "status": "ok",
                "data": {"roles": role_meta()},
            })
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_command_presets(self) -> Any:
        """返回「扩写提示词」「提取图片形象」两个独立分区的当前配置。"""
        try:
            stores = self.command_stores() if callable(self.command_stores) else (self.command_stores or {})
            payload = {}
            for section in ("expand", "extract"):
                store = stores.get(section)
                payload[section] = store.list_items() if store is not None else []
            payload["providers"] = list_astrbot_providers(self.context)
            logger.info(
                "[neko_draw] 指令预设就绪：扩写 %s 条 / 提取 %s 条",
                len(payload["expand"]),
                len(payload["extract"]),
            )
            return json_response({"status": "ok", "data": payload})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_save_command_presets(self) -> Any:
        """保存一个分区（expand / extract）的全部条目，并立即生效。"""
        try:
            body = await request.json() if hasattr(request, "json") else {}
            if not isinstance(body, dict):
                return error_response("缺少有效的请求数据", status_code=400)
            section = str(body.get("section", "") or "").strip().lower()
            if section not in ("expand", "extract"):
                return error_response("无效的指令分区", status_code=400)
            items = body.get("items")
            stores = self.command_stores() if callable(self.command_stores) else (self.command_stores or {})
            store = stores.get(section)
            if store is None:
                return error_response("指令分区尚未初始化", status_code=503)
            # 失败时回滚：replace_all 会先改内存和本地文件，再写配置。
            # 配置写盘失败若只返回 500，内存/本地/配置三处会各不相同。
            previous_items = store.list_items()
            previous_config = None
            if isinstance(self.config, dict):
                previous_config = deepcopy(self.config.get(store.config_key))
            try:
                store.replace_all(items)
            except Exception:
                store.replace_all(previous_items)
                if previous_config is not None:
                    self.config[store.config_key] = previous_config
                raise
            return json_response({"status": "ok", "data": {"items": store.list_items()}})
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_list_llm_history(self) -> Any:
        """扩写 / 提取的对话模型调用历史（列表，不含大字段）。"""
        try:
            store = self._llm_history_store()
            if store is None:
                return json_response({"status": "ok", "data": {"items": [], "total": 0, "page": 1, "page_size": 20}})
            args = {}
            if hasattr(request, "query") and request.query:
                args = dict(request.query)
            elif hasattr(request, "args"):
                args = dict(request.args)

            def num(name, default=0):
                try:
                    return type(default)(args.get(name, default))
                except (TypeError, ValueError):
                    return default

            data = store.list(
                page=max(1, num("page", 1)),
                page_size=max(1, min(100, num("page_size", 20))),
                section=str(args.get("section", "") or ""),
                status=str(args.get("status", "") or ""),
                source=str(args.get("source", "") or ""),
                start_time=num("start_time", 0.0),
                end_time=num("end_time", 0.0),
            )
            return json_response({"status": "ok", "data": data})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_get_llm_history(self, record_id: str) -> Any:
        try:
            store = self._llm_history_store()
            item = store.get(int(record_id)) if store is not None else None
            if item is None:
                return error_response("记录不存在", status_code=404)
            return json_response({"status": "ok", "data": item})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_llm_history_stats(self) -> Any:
        try:
            store = self._llm_history_store()
            data = store.stats() if store is not None else {"total": 0, "success": 0, "failed": 0, "avg_ms": 0, "by_section": [], "by_model": []}
            return json_response({"status": "ok", "data": data})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_clear_llm_history(self) -> Any:
        try:
            store = self._llm_history_store()
            removed = store.clear() if store is not None else 0
            return json_response({"status": "ok", "data": {"removed": removed}})
        except Exception as e:
            return error_response(str(e), status_code=500)

    async def api_delete_llm_history(self) -> Any:
        try:
            body = await request.json() if hasattr(request, "json") else {}
            record_id = int((body or {}).get("id") or 0)
            store = self._llm_history_store()
            ok = store.delete(record_id) if store is not None else False
            return json_response({"status": "ok", "data": {"deleted": bool(ok)}})
        except Exception as e:
            return error_response(str(e), status_code=500)

    def _llm_history_store(self):
        store = self.llm_history() if callable(self.llm_history) else self.llm_history
        return store

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

    async def api_provider_balances(self) -> Any:
        """逐个查询提供商各 Key 的余额；不提交生图任务。"""
        try:
            body = await request.json() if hasattr(request, "json") else {}
            item = body.get("provider") if isinstance(body, dict) else None
            if not isinstance(item, dict):
                return error_response("缺少有效的模型提供商配置", status_code=400)
            data = await fetch_provider_balances(item, self.context)
            return json_response({"status": "ok", "data": data})
        except (ValueError, asyncio.TimeoutError) as e:
            return error_response("余额查询超时" if isinstance(e, asyncio.TimeoutError) else str(e), status_code=400)
        except Exception as e:
            return error_response(f"余额查询失败：{type(e).__name__}", status_code=502)

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
            keyword = args.get("keyword", "")
            order = args.get("order", "time_desc")

            result = self.history.list(
                page=page,
                page_size=page_size,
                user_id=user_id,
                model_template=model_template,
                status=status,
                start_time=start_time,
                end_time=end_time,
                keyword=keyword,
                order=order,
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

    async def api_recall_history(self) -> Any:
        """按历史记录里的任务 ID 到平台取回生成图，并补发到原会话。"""
        try:
            if self.recall_handler is None:
                return error_response("找回功能未启用", status_code=503)
            body = await request.json() if hasattr(request, "json") else {}
            record_id = body.get("id") if isinstance(body, dict) else None
            if record_id is None:
                return error_response("缺少 id", status_code=400)
            deliver = bool(body.get("deliver", True))
            data = await self.recall_handler(int(record_id), deliver)
            message = "已找回并补发" if data.get("delivered") else "已找回"
            if not data.get("deliver_available"):
                message = "已找回；该记录缺少会话信息，未补发"
            data = dict(data)
            data["message"] = message
            return json_response({"status": "ok", "message": message, "data": data})
        except ValueError as e:
            return error_response(str(e), status_code=400)
        except Exception as e:
            return error_response(f"找回失败：{type(e).__name__}", status_code=500)

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
