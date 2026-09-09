# -*- coding: utf-8 -*-
"""预设提示词管理器。

预设提示词保存在配置中（首次运行写入数据目录 JSON），支持
/nc pa、/nc pd、/nc pl、/nc ps 指令动态维护。

内部统一存储为 dict 列表：[{"trigger": str, "prompt": str}, ...]
兼容旧版字符串列表格式（自动迁移为 dict）。

加载优先级：
1. WebUI 配置（_conf_schema.json 中的 prompt 字段）—— 最高优先，
   加载时同步写入 prompts.json，确保面板修改即时生效
2. prompts.json —— 仅当 WebUI 配置为空时使用，存放 /nc pa 等指令变更
   命令动态维护的预设
3. DEFAULT_PROMPTS —— 兜底默认值
"""
import json
import shlex
from pathlib import Path
from typing import Optional
from .storage import read_json, write_json

DEFAULT_PROMPTS = [
    {"trigger": "nd", "prompt": "{{user_text}}", "reference_image": [], "reference_image_position": "before"},
    {"trigger": "nde", "prompt": "{{user_text}} --model edit", "reference_image": [], "reference_image_position": "before"},
]


def _normalize_item(item) -> Optional[dict]:
    """将配置项归一化为 {"trigger": str, "prompt": str, "reference_image": ..., "reference_image_position": str} 字典。

    兼容两种输入格式：
    - 新格式 dict: {"trigger": "...", "prompt": "...", "reference_image": [...], "reference_image_position": "before"}
    - 旧格式 str: "trigger prompt_template --params"
    """
    if isinstance(item, dict):
        trigger = str(item.get("trigger", "")).strip()
        prompt = str(item.get("prompt", "")).strip()
        reference_image = item.get("reference_image", [])
        reference_image_position = str(item.get("reference_image_position", "before")).strip().lower()
        if reference_image_position not in ("before", "after"):
            reference_image_position = "before"
        if trigger:
            result = {
                "trigger": trigger,
                "prompt": prompt,
                "reference_image": reference_image,
                "reference_image_position": reference_image_position,
            }
            # 原项已带 __template_key 时原样保留，避免命令增删写回时丢失标识；
            # 旧项缺失时不强行补（由 WebUI 渲染层与运行时按模板兜底）。
            template_key = item.get("__template_key")
            if template_key:
                result["__template_key"] = template_key
            return result
        return None
    if isinstance(item, str) and item.strip():
        s = item.strip()
        tokens = s.split(None, 1)
        if tokens:
            trigger = tokens[0]
            prompt = " ".join(tokens[1:]) if len(tokens) > 1 else ""
            return {"trigger": trigger, "prompt": prompt, "reference_image": [], "reference_image_position": "before"}
    return None


class PromptManager:
    def __init__(self, config_prompts, data_dir: str | Path, config=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.data_dir / "prompts.json"
        self.config = config
        self._snapshot = self.data_dir / 'prompts_config_snapshot.json'
        self.prompts = self._load(config_prompts)

    def _load(self, config_prompts) -> list[dict]:
        """加载预设：WebUI 配置与本地 prompts.json 合并，命令添加的预设重启不丢失。

        合并规则：
        - WebUI 配置中的预设为权威来源（同触发词时覆盖本地）
        - 本地 prompts.json 中独有的预设（通过 /nc pa 指令添加）予以保留
        - 合并结果回写 prompts.json，确保命令维护的预设持久化
        """
        # 1. 归一化 WebUI 配置
        webui_items: dict[str, dict] = {}
        if config_prompts:
            for item in config_prompts:
                norm = _normalize_item(item)
                if norm is not None:
                    webui_items[norm["trigger"]] = norm

        # 2. 加载本地 prompts.json（命令动态维护的结果）
        local_items: dict[str, dict] = {}
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    for item in data:
                        norm = _normalize_item(item)
                        if norm is not None:
                            local_items[norm["trigger"]] = norm
            except Exception:
                pass

        # 3. 合并：本地独有预设保留，WebUI 预设覆盖同触发词
        merged: dict[str, dict] = {}
        previous = read_json(self._snapshot, [])
        removed = set(previous) - set(webui_items)
        merged.update({k: v for k, v in local_items.items() if k not in removed})
        merged.update(webui_items)  # WebUI 优先
        write_json(self._snapshot, list(webui_items))

        if merged:
            result = list(merged.values())
            try:
                write_json(self._file, result)
            except Exception:
                pass
            return result

        # 4. 都没有，用默认值
        if self._file.exists() or config_prompts is not None:
            write_json(self._file, [])
            return []
        return [dict(x) for x in DEFAULT_PROMPTS]

    def _save(self) -> None:
        write_json(self._file, self.prompts)
        if self.config is not None:
            self.config['prompt'] = list(self.prompts)
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            write_json(self._snapshot, [p['trigger'] for p in self.prompts])

    def promote_to_config(self, config_prompts) -> list:
        """非破坏性地把命令动态维护、但配置中缺失的预设补充进配置列表。

        与"整体用归一化结果覆盖"不同，这里以磁盘原始 config 为基底：
        - 用户已有项（含字符串旧预设、__template_key、reference_image 等全部字段）原样保留；
        - 仅追加运行时合并得到、而 config 中不存在同触发词的命令新增预设；
        - 同步把当前生效触发词写入快照，使后续在面板删除命令预设具备权威性；
        - 不调用 save_config，不在插件启动时改写用户磁盘配置文件。
        """
        base = [dict(item) if isinstance(item, dict) else item for item in (config_prompts or [])]
        existing = set()
        for item in base:
            if isinstance(item, dict):
                trigger = item.get("trigger", "")
                if trigger:
                    existing.add(trigger)
            elif isinstance(item, str):
                # 字符串旧预设同样占用其首词触发词，避免归一化后被当作命令新增重复追加
                norm = _normalize_item(item)
                if norm is not None:
                    existing.add(norm["trigger"])
        for item in self.prompts:
            trigger = item.get("trigger", "")
            if trigger and trigger not in existing:
                base.append(dict(item))
                existing.add(trigger)
        # 快照记录已暴露给面板的全部生效触发词（含命令提升项），供下次启动判定面板删除
        write_json(self._snapshot, [p.get("trigger", "") for p in self.prompts if p.get("trigger")])
        return base

    @staticmethod
    def get_trigger(item: dict) -> str:
        return item.get("trigger", "")

    def add(self, trigger: str, prompt_body: str) -> tuple[bool, str]:
        """添加预设。触发词已存在时覆盖更新。"""
        trigger = trigger.strip()
        prompt_body = prompt_body.strip()
        if not trigger:
            return False, "用法：/nc pa <触发词> <提示词内容>"
        new_item = {"trigger": trigger, "prompt": prompt_body, "reference_image": [], "reference_image_position": "before", "__template_key": "prompt_item"}
        for i, p in enumerate(self.prompts):
            if self.get_trigger(p) == trigger:
                # 保留已有的 reference_image 和 position（命令行添加不覆盖 WebUI 配置）
                new_item["reference_image"] = p.get("reference_image", [])
                new_item["reference_image_position"] = p.get("reference_image_position", "before")
                # 已有标识则沿用，避免更新时把模板标识抹掉
                if p.get("__template_key"):
                    new_item["__template_key"] = p["__template_key"]
                self.prompts[i] = new_item
                self._save()
                return True, f"已更新预设：{trigger}"
        self.prompts.append(new_item)
        self._save()
        return True, f"已添加预设：{trigger}"

    def delete(self, trigger: str) -> tuple[bool, str]:
        trigger = trigger.strip()
        for i, p in enumerate(self.prompts):
            if self.get_trigger(p) == trigger:
                self.prompts.pop(i)
                self._save()
                return True, f"已删除预设：{trigger}"
        return False, f"未找到触发词：{trigger}"

    def list_prompts(self) -> list[str]:
        return [self.get_trigger(p) for p in self.prompts]

    def get_detail(self, trigger: str) -> Optional[str]:
        """返回 "trigger prompt" 格式的完整字符串，用于命令展示。"""
        trigger = trigger.strip()
        for p in self.prompts:
            if self.get_trigger(p) == trigger:
                return f"{p['trigger']} {p['prompt']}".strip()
        return None

    def get_all(self) -> list[str]:
        """返回字符串列表，供 parser.parse_prompt_message 匹配使用。

        每项格式："trigger prompt_template"，与旧版完全兼容。
        """
        return [f"{p['trigger']} {p['prompt']}".strip() for p in self.prompts]

    def get_reference_image_for_text(self, text: str):
        """根据消息文本匹配预设，返回 (参考图原始配置值, 位置) 元组。

        匹配逻辑与 parser.parse_prompt_message 一致：最长触发词优先。
        参考图值可能是 list（file 上传类型）、str（旧版手动路径）或 None（未匹配/无参考图）。
        位置为 "before" 或 "after"。
        返回 None 表示未匹配到有参考图的预设。
        """
        if not text or not self.prompts:
            return None
        candidates = []
        for p in self.prompts:
            trigger = self.get_trigger(p)
            if not trigger:
                continue
            if text == trigger or text.startswith(trigger + " "):
                ref = p.get("reference_image", [])
                if ref:
                    position = str(p.get("reference_image_position", "before")).strip().lower()
                    if position not in ("before", "after"):
                        position = "before"
                    candidates.append((len(trigger), ref, position))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1], candidates[0][2]
