# -*- coding: utf-8 -*-
"""绘图核心业务逻辑（与 AstrBot 事件层解耦，可独立测试）。

处理流程：
  匹配预设触发词 -> 解析提示词与参数 -> 选择模型模板（--model / 默认）
  -> 收集参考图（按模板 refer_field）-> 提交 WaveSpeed 任务 -> 轮询 -> 下载结果
"""
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from .downloader import Downloader
from .openapi import OpenAPIError
from .parser import ParsedPrompt, parse_prompt_message
from .prompt_manager import PromptManager
from .runninghub import RunningHubError, RunningHubPending
from .templates import SCALAR_REFER_FIELDS, ModelTemplate, TemplateManager, build_payload
from .wavespeed import WavespeedError
from .whitelist import WhitelistGuard

logger = logging.getLogger("neko_draw")

# 进度阶段
STAGE_START = "start"
ProgressCallback = Callable[[str], Awaitable[None]]

# --model 别名：text/edit 指向默认文生图/编辑模板
MODEL_ALIAS_TEXT = "text"
MODEL_ALIAS_EDIT = "edit"


@dataclass
class PendingGeneration:
    """已提交但排队未完成的任务（超时后由事件层转入后台补发）。"""

    client: object
    provider: str
    model: str
    task_id: str
    key: Optional[str] = None


@dataclass
class HandlerResult:
    images: list[str] = field(default_factory=list)   # 本地图片路径
    text: str = ""                                     # 文本回复
    silent: bool = False                               # True: 静默返回（无权限等）
    # 生成元数据（用于历史记录）
    trigger: str = ""
    model_template: str = ""
    provider: str = ""
    model: str = ""
    params: dict = field(default_factory=dict)
    refer_image_count: int = 0
    task_id: str = ""                                  # RunningHub 任务 ID（便于追溯）
    resolved_prompt: str = ""                          # 实际发给模型的提示词（含 LLM 拆解结果）
    pending: Optional["PendingGeneration"] = None      # 排队超时的任务，转后台补发
    # 命中预设时，该预设的 APNG 三态（"" 跟随全局 / "on" / "off"），供事件层与历史记录使用
    apng: str = ""
    # 本次生效的预设触发词（拆解失败等场景也据此计算 APNG 等预设选项）
    preset_trigger: str = ""


class DrawingHandler:
    def __init__(
        self,
        *,
        templates: TemplateManager,
        default_text_model: str,
        default_edit_model: str,
        prompt_manager: PromptManager,
        whitelist_guard: WhitelistGuard,
        downloader: Downloader,
        providers: dict,
    ):
        """
        providers: {provider_name: client_object}，每个 client 需实现
                   async generate(model, payload) -> list[str]
        """
        self.templates = templates
        self.default_text_model = default_text_model
        self.default_edit_model = default_edit_model
        self.prompts = prompt_manager
        self.whitelist = whitelist_guard
        self.downloader = downloader
        self.providers = providers

    def _result_with_meta(
        self,
        parsed: ParsedPrompt,
        template: ModelTemplate,
        refer_uris: list[str],
        **kwargs,
    ) -> HandlerResult:
        """构建带生成元数据的结果（用于历史记录）。"""
        return HandlerResult(
            trigger=parsed.trigger,
            model_template=template.name,
            provider=template.provider,
            model=template.model,
            params=dict(parsed.params),
            # 单图字段（image/init_image/input_image）实际只提交第一张，
            # 历史里的“参考图 N”要跟真实请求一致，不能把丢弃的张数也算进去。
            refer_image_count=(
                1 if refer_uris and template.refer_field.lower() in SCALAR_REFER_FIELDS
                else len(refer_uris)
            ),
            resolved_prompt=parsed.text,
            apng=str((self.prompts.get_by_trigger(parsed.trigger) or {}).get("apng", "") or ""),
            preset_trigger=parsed.trigger,
            **kwargs,
        )

    @staticmethod
    def _validate_as_default(template: Optional[ModelTemplate]) -> bool:
        """预设选中的模型能否作为该场景的默认模型（启用 + 允许默认）。"""
        return bool(
            template is not None
            and template.enabled
            and template.enabled_as_default
        )

    def _resolve_template(
        self,
        params: dict,
        has_images: bool,
        preset_model: str = "",
        user_model: str = "",
    ) -> tuple[Optional[ModelTemplate], str]:
        """选择模型模板，返回 (模板, 模式标识)。

        优先级（低 → 高）：
        全局默认模型 < 预设「默认模型」< 预设提示词模板里的 --model < 用户消息里的 --model。
        因此预设的「默认模型」只决定“默认”，用户/预设模板显式写的 --model 依旧优先；
        预设默认模型不是“文生图 / 图片编辑”用途匹配的模型时，回落到全局默认。
        """
        def configured_default(name: str, image_mode: bool) -> Optional[ModelTemplate]:
            if not str(name or "").strip():
                return None
            template = self.templates.resolve(name)
            if (
                template is not None
                and template.enabled_as_default
                and bool(template.refer_field) is image_mode
            ):
                return template
            return self.templates.fallback(image_mode)

        def pick(name: str, image_mode: bool) -> Optional[ModelTemplate]:
            preset_template = self.templates.resolve(name) if name else None
            if (
                preset_template is not None
                and self._validate_as_default(preset_template)
                and bool(preset_template.refer_field) is image_mode
            ):
                return preset_template
            if name:
                logger.info(
                    "[IMAGE] 预设默认模型「%s」不可用（未启用 / 不允许作默认 / 用途不匹配），改用全局默认",
                    name,
                )
            return configured_default(
                self.default_edit_model if image_mode else self.default_text_model,
                image_mode,
            )

        model_flag = str(params.get("model", "") or "").strip()
        # 参数值本身与默认模型相同（来自预设固定参数或用户显式指定）也算显式选择
        explicitly_set = bool(
            str(user_model or "").strip() or str(params.get("model", "") or "").strip()
        )
        # 别名：text / edit
        if model_flag == MODEL_ALIAS_TEXT:
            return configured_default(self.default_text_model, False), MODEL_ALIAS_TEXT
        if model_flag == MODEL_ALIAS_EDIT:
            return configured_default(self.default_edit_model, True), MODEL_ALIAS_EDIT
        # 显式模板名
        if model_flag:
            t = self.templates.resolve(model_flag)
            return (t, model_flag)
        # 默认：带图走编辑模板，无图走文生图模板
        if has_images:
            if not explicitly_set and preset_model:
                return pick(preset_model, True), MODEL_ALIAS_EDIT
            return configured_default(self.default_edit_model, True), MODEL_ALIAS_EDIT
        if not explicitly_set and preset_model:
            return pick(preset_model, False), MODEL_ALIAS_TEXT
        return configured_default(self.default_text_model, False), MODEL_ALIAS_TEXT

    def describe_template(self, params: dict, has_images: bool) -> str:
        """给出“为什么没有可用模型”的可读原因（用于报错，不暴露内部 ID）。"""
        model_flag = str(params.get("model", "") or "").strip()
        if not model_flag or model_flag in (MODEL_ALIAS_TEXT, MODEL_ALIAS_EDIT):
            image_mode = has_images if not model_flag else model_flag == MODEL_ALIAS_EDIT
            role = "图片编辑" if image_mode else "文生图"
            return (
                f"当前没有可用的{role}模型（模型被停用、取消了“允许作默认”，"
                f"或本轮请求用途不匹配），请在面板中检查模型设置"
            )
        if self.templates.resolve(model_flag) is None:
            return f"指定的模型「{model_flag}」不存在或已停用"
        return f"模型「{model_flag}」不可用"

    async def _collect_refer_data_uris(
        self, image_urls: list[str], max_images: int
    ) -> list[str]:
        """把参考图 URL 全部转成 data URI，最多 max_images 张。"""
        uris: list[str] = []
        for url in image_urls[:max(0, max_images)]:
            uri = await self.downloader.download_to_data_uri(url)
            if uri:
                uris.append(uri)
            else:
                logger.warning("参考图下载失败，已跳过: %s", url)
        return uris

    async def handle(
        self,
        text: str,
        image_urls: list[str] | None = None,
        *,
        user_id: str = "",
        group_id: str = "",
        progress_cb: Optional[ProgressCallback] = None,
        user_text_override: Optional[str] = None,
    ) -> HandlerResult:
        """处理一条绘图消息。

        progress_cb 在关键阶段被回调（STAGE_START=开始生成），
        事件层可借此发送"开始画图"提示。
        user_text_override 不为 None 时，用它替换提示词模板里的 {{user_text}}
        （用于 LLM 拆解 tag），触发词与 --参数 仍按原消息解析。
        """
        # 1. 白名单
        if not self.whitelist.check(user_id, group_id):
            return HandlerResult(silent=True)

        # 2. 匹配预设触发词
        parsed: Optional[ParsedPrompt] = parse_prompt_message(
            text, self.prompts.get_all(), user_text_override=user_text_override
        )
        if parsed is None:
            return HandlerResult(silent=True)

        # 2.5 套用预设的固定参数。
        # 优先级（低 → 高）：预设固定参数 < 预设提示词模板里的 --参数 < 用户消息里的 --参数。
        # 因此这里把预设固定参数垫在解析结果之下，用户消息里的同名 --参数天然覆盖。
        # 预设「默认模型」不写进参数，而是交给 _resolve_template 作为“默认值”，
        # 这样用户消息里显式写的 --model 依旧优先（旧实现会把它静默忽略）。
        preset = self.prompts.get_by_trigger(parsed.trigger)
        preset_model = ""
        if isinstance(preset, dict):
            preset_params = preset.get("params")
            if isinstance(preset_params, dict) and preset_params:
                merged = {str(k): v for k, v in preset_params.items() if str(k).strip() and str(k) != "model"}
                merged.update(parsed.params)
                parsed.params = merged
            preset_model = str(preset.get("model", "") or "").strip()

        image_urls = image_urls or []
        has_images = bool(image_urls)

        # 3. 选择模型模板（全局默认 < 预设默认模型 < 显式 --model）
        user_model = str((parsed.user_params or {}).get("model", "") or "").strip()
        template, mode = self._resolve_template(
            parsed.params, has_images, preset_model=preset_model, user_model=user_model
        )
        if template is None:
            available = "、".join(self.templates.enabled_names()) or "（无可用模板）"
            return HandlerResult(
                text=f"{self.describe_template(parsed.params, has_images)}。"
                     f"可用模型：{available}；也可用 --model <模型名> 指定",
                preset_trigger=parsed.trigger,
            )

        # 提示词为空保护：必须在选出模板之后判断。
        # 图片编辑模型（模板有 refer_field）在“只发图 + 触发词”时，图片本身就是指令，
        # 允许正文为空；以前在选模型之前无条件拦下，导致合并转发取图/纯图改图必然
        # 报“提示词不能为空”。
        if not parsed.text.strip() and not (has_images and template.refer_field):
            if parsed.user_body or user_text_override:
                # 模板本身不含正文且用户也没给正文：这是预设配置问题，提示得更明确
                return HandlerResult(
                    text=f"预设「{parsed.trigger}」的提示词模板渲染后为空，请检查该预设的提示词模板",
                    preset_trigger=parsed.trigger,
                )
            return HandlerResult(
                text="提示词不能为空。示例：nd 一只戴帽子的猫 --aspect_ratio 16:9",
                preset_trigger=parsed.trigger,
            )

        # 4. 参考图处理（按模板 refer_field）
        if template.refer_field and not has_images:
            return HandlerResult(
                text=f"模型「{template.name}」需要参考图（字段 {template.refer_field}）。"
                     f"请发送图片或引用一张图片",
                preset_trigger=parsed.trigger,
            )
        refer_uris: list[str] = []
        if has_images:
            if not template.refer_field:
                logger.info(
                    "[IMAGE] 模板 %s 不支持参考图，忽略 %d 张图片",
                    template.name, len(image_urls),
                )
            else:
                if template.max_refer_images <= 0:
                    # 上限 0 时一张都不会取：必须说清是配置问题，否则用户会去查图片链接
                    return HandlerResult(
                        text=f"模型「{template.name}」的「参考图数量上限」是 0，"
                             f"所以发图也不会被使用。请在「模型提供商 → 该模型」里把上限改成大于 0",
                        preset_trigger=parsed.trigger,
                    )
                refer_uris = await self._collect_refer_data_uris(
                    image_urls, template.max_refer_images
                )
                if not refer_uris:
                    return HandlerResult(
                        text="参考图全部下载失败，请检查图片链接是否有效",
                        preset_trigger=parsed.trigger,
                    )

        # 4.5 提示词长度校验（按模板 min_prompt_length）
        if template.min_prompt_length > 0:
            prompt_len = len(parsed.text.strip())
            if prompt_len < template.min_prompt_length:
                return HandlerResult(
                    text=f"提示词太短（当前 {prompt_len} 字符），模型「{template.name}」"
                         f"要求至少 {template.min_prompt_length} 个字符。请补充描述后重试",
                    preset_trigger=parsed.trigger,
                )

        # 5. 构造 payload 并提交
        payload = build_payload(
            parsed.text, parsed.params, template, refer_uris
        )
        # 选择 Provider
        provider_client = self.providers.get(template.provider)
        if provider_client is None:
            available = "、".join(self.providers.keys()) or "（无）"
            return HandlerResult(
                text=f"模型提供商「{template.provider}」未配置。可用模型提供商：{available}",
                preset_trigger=parsed.trigger,
            )
        logger.info(
            "[IMAGE] 触发词=%s 模板=%s 模型提供商=%s 模型=%s 参考图=%d 参数=%s",
            parsed.trigger, template.name, template.provider, template.model,
            len(refer_uris), parsed.params,
        )
        # 6. 开始生成前回调（事件层发送"开始画图"提示）
        if progress_cb is not None:
            try:
                await progress_cb(STAGE_START)
            except Exception:  # noqa: BLE001
                logger.exception("进度回调执行失败（不影响生成）")
        task_id_box: dict[str, str] = {}

        def _capture_task_id(task_id: str) -> None:
            task_id_box["task_id"] = str(task_id)

        try:
            output_urls = await provider_client.generate(
                template.model, payload, on_task_id=_capture_task_id
            )
        except RunningHubPending as e:
            # 任务已提交、只是还没跑完：不当作失败，转后台继续等待并补发原图。
            logger.info(
                "[IMAGE] 任务排队超时，转入后台等待：任务ID=%s", e.task_id,
            )
            return self._result_with_meta(
                parsed, template, refer_uris,
                task_id=e.task_id,
                pending=PendingGeneration(
                    client=provider_client,
                    provider=template.provider,
                    model=template.model,
                    task_id=e.task_id,
                    key=e.key,
                ),
            )
        except (WavespeedError, RunningHubError, OpenAPIError) as e:
            detail = str(e)
            if "超时" in detail or "timeout" in detail.lower():
                detail = "小猫/ᐠ - ˕ -マ Ⳋ提醒你"
            return self._result_with_meta(
                parsed, template, refer_uris,
                text=detail if detail.startswith("小猫/") else f"图片生成失败：{detail}",
                task_id=task_id_box.get("task_id", ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("生成过程异常")
            detail = str(e)
            if isinstance(e, TimeoutError) or "超时" in detail or "timeout" in detail.lower():
                detail = "小猫/ᐠ - ˕ -マ Ⳋ提醒你"
            return self._result_with_meta(
                parsed, template, refer_uris,
                text=detail if detail.startswith("小猫/") else f"图片生成异常：{detail}",
                task_id=task_id_box.get("task_id", ""),
            )

        # 7. 下载结果
        if not output_urls:
            return self._result_with_meta(
                parsed, template, refer_uris,
                text="生成完成，但未返回图片",
                task_id=task_id_box.get("task_id", ""),
            )
        local_paths: list[str] = []
        for url in output_urls:
            path = await self.downloader.download(url, proxy=getattr(provider_client, "proxy", None))
            if path:
                local_paths.append(path)
        if not local_paths:
            return self._result_with_meta(
                parsed, template, refer_uris,
                text="生成完成，但图片下载失败",
                task_id=task_id_box.get("task_id", ""),
            )
        return self._result_with_meta(
            parsed, template, refer_uris,
            images=local_paths,
            task_id=task_id_box.get("task_id", ""),
        )
