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
from .runninghub import RunningHubError
from .templates import ModelTemplate, TemplateManager, build_payload
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
            refer_image_count=len(refer_uris),
            **kwargs,
        )

    def _resolve_template(
        self, params: dict, has_images: bool
    ) -> tuple[Optional[ModelTemplate], str]:
        """选择模型模板，返回 (模板, 模式标识)。"""
        model_flag = str(params.get("model", "") or "").strip()
        # 别名：text / edit
        if model_flag == MODEL_ALIAS_TEXT:
            t = self.templates.resolve(self.default_text_model)
            return (t, MODEL_ALIAS_TEXT)
        if model_flag == MODEL_ALIAS_EDIT:
            t = self.templates.resolve(self.default_edit_model)
            return (t, MODEL_ALIAS_EDIT)
        # 显式模板名
        if model_flag:
            t = self.templates.resolve(model_flag)
            return (t, model_flag)
        # 默认：带图走编辑模板，无图走文生图模板
        if has_images:
            t = self.templates.resolve(self.default_edit_model)
            if t is not None:
                return t, MODEL_ALIAS_EDIT
        t = self.templates.resolve(self.default_text_model)
        return (t, MODEL_ALIAS_TEXT)

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
    ) -> HandlerResult:
        """处理一条绘图消息。

        progress_cb 在关键阶段被回调（STAGE_START=开始生成），
        事件层可借此发送"开始画图"提示。
        """
        # 1. 白名单
        if not self.whitelist.check(user_id, group_id):
            return HandlerResult(silent=True)

        # 2. 匹配预设触发词
        parsed: Optional[ParsedPrompt] = parse_prompt_message(
            text, self.prompts.get_all()
        )
        if parsed is None:
            return HandlerResult(silent=True)

        image_urls = image_urls or []
        has_images = bool(image_urls)

        # 提示词为空保护
        if not parsed.text.strip():
            return HandlerResult(
                text="提示词不能为空。示例：猫娘画图 一只戴帽子的猫 --aspect_ratio 16:9"
            )

        # 3. 选择模型模板
        template, mode = self._resolve_template(parsed.params, has_images)
        if template is None:
            available = "、".join(self.templates.enabled_names()) or "（无可用模板）"
            return HandlerResult(
                text=f"模型模板不可用。可用模板：{available}；"
                     f"可用 --model <模板名> 指定，或在插件配置中添加模板"
            )

        # 4. 参考图处理（按模板 refer_field）
        if template.refer_field and not has_images:
            return HandlerResult(
                text=f"模型「{template.name}」需要参考图（字段 {template.refer_field}）。"
                     f"请发送图片或引用一张图片"
            )
        refer_uris: list[str] = []
        if has_images:
            if not template.refer_field:
                logger.info(
                    "[IMAGE] 模板 %s 不支持参考图，忽略 %d 张图片",
                    template.name, len(image_urls),
                )
            else:
                refer_uris = await self._collect_refer_data_uris(
                    image_urls, template.max_refer_images
                )
                if not refer_uris:
                    return HandlerResult(text="参考图全部下载失败，请检查图片链接是否有效")

        # 4.5 提示词长度校验（按模板 min_prompt_length）
        if template.min_prompt_length > 0:
            prompt_len = len(parsed.text.strip())
            if prompt_len < template.min_prompt_length:
                return HandlerResult(
                    text=f"提示词太短（当前 {prompt_len} 字符），模型「{template.name}」"
                         f"要求至少 {template.min_prompt_length} 个字符。请补充描述后重试"
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
                text=f"提供商「{template.provider}」未配置。可用提供商：{available}"
            )
        logger.info(
            "[IMAGE] 触发词=%s 模板=%s 提供商=%s 模型=%s 参考图=%d 参数=%s",
            parsed.trigger, template.name, template.provider, template.model,
            len(refer_uris), parsed.params,
        )
        # 6. 开始生成前回调（事件层发送"开始画图"提示）
        if progress_cb is not None:
            try:
                await progress_cb(STAGE_START)
            except Exception:  # noqa: BLE001
                logger.exception("进度回调执行失败（不影响生成）")
        try:
            output_urls = await provider_client.generate(template.model, payload)
        except (WavespeedError, RunningHubError, OpenAPIError) as e:
            return self._result_with_meta(
                parsed, template, refer_uris,
                text=f"图片生成失败：{e}",
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("生成过程异常")
            return self._result_with_meta(
                parsed, template, refer_uris,
                text=f"图片生成异常：{e}",
            )

        # 7. 下载结果
        if not output_urls:
            return self._result_with_meta(
                parsed, template, refer_uris,
                text="生成完成，但未返回图片",
            )
        local_paths: list[str] = []
        for url in output_urls:
            path = await self.downloader.download(url)
            if path:
                local_paths.append(path)
        if not local_paths:
            return self._result_with_meta(
                parsed, template, refer_uris,
                text="生成完成，但图片下载失败",
            )
        return self._result_with_meta(
            parsed, template, refer_uris,
            images=local_paths,
        )
