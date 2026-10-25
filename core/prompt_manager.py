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
    {"trigger": "nd", "prompt": "{{user_text}}", "reference_image": [], "reference_image_position": "before", "model": "", "params": {}},
    {"trigger": "nde", "prompt": "{{user_text}} --model edit", "reference_image": [], "reference_image_position": "before", "model": "", "params": {}},
]


def _normalize_params(raw) -> dict:
    """把预设的固定参数归一化为 {str: value}；非法输入返回空字典。"""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if str(k).strip()}
    return {}


def _normalize_choice(raw) -> str:
    """把三态开关归一化为 ""（跟随全局）/ "on" / "off"。"""
    value = str(raw or "").strip().lower()
    return value if value in ("on", "off") else ""


def _normalize_item(item) -> Optional[dict]:
    """将配置项归一化为预设字典。

    字段：trigger / prompt / model / params / apng / decompose / tag_prompt /
    reference_image / reference_image_position。

    ``tag_prompt`` 现在存的是「生图预处理用哪条扩写指令」的触发词，
    不再是提示词模板本身。

    兼容两种输入格式：
    - 新格式 dict: {"trigger": "...", "prompt": "...", "model": "...", "params": {...}, "reference_image": [...]}
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
                "enabled": item.get("enabled", True) is not False,
                "prompt": prompt,
                "model": str(item.get("model", "")).strip(),
                "params": _normalize_params(item.get("params")),
                "apng": _normalize_choice(item.get("apng")),
                "decompose": _normalize_choice(item.get("decompose")),
                # 预设级「拆解用户提示词模板」：留空表示跟随全局，填写则覆盖全局模板
                "tag_prompt": str(item.get("tag_prompt", "") or "").strip(),
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
            return {"trigger": trigger, "prompt": prompt, "model": "", "params": {},
                    "apng": "", "decompose": "", "tag_prompt": "",
                    "reference_image": [], "reference_image_position": "before"}
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
        new_item = {"trigger": trigger, "prompt": prompt_body, "model": "", "params": {},
                    "apng": "", "decompose": "", "tag_prompt": "",
                    "reference_image": [], "reference_image_position": "before", "__template_key": "prompt_item"}
        for i, p in enumerate(self.prompts):
            if self.get_trigger(p) == trigger:
                # 保留已有的参考图/位置/默认模型/固定参数/APNG/拆解/拆解模板（命令行添加不覆盖 WebUI 配置）
                new_item["reference_image"] = p.get("reference_image", [])
                new_item["reference_image_position"] = p.get("reference_image_position", "before")
                new_item["model"] = str(p.get("model", "") or "").strip()
                new_item["params"] = _normalize_params(p.get("params"))
                new_item["apng"] = _normalize_choice(p.get("apng"))
                new_item["decompose"] = _normalize_choice(p.get("decompose"))
                new_item["tag_prompt"] = str(p.get("tag_prompt", "") or "").strip()
                # 停用状态必须一并保留：命令行改提示词不应该把用户停用的预设重新启用
                new_item["enabled"] = p.get("enabled", True) is not False
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
        """返回预设详情（触发词 + 提示词模板 + 默认模型 + 固定参数），用于命令展示。"""
        trigger = trigger.strip()
        for p in self.prompts:
            if self.get_trigger(p) == trigger:
                lines = [f"{p['trigger']} {p.get('prompt', '')}".strip()]
                model = str(p.get("model", "") or "").strip()
                if model:
                    lines.append(f"默认模型：{model}")
                params = p.get("params") or {}
                if isinstance(params, dict) and params:
                    lines.append("固定参数：" + json.dumps(params, ensure_ascii=False))
                apng = str(p.get("apng", "") or "").strip()
                if apng:
                    lines.append("APNG 包装：" + ("转成 APNG" if apng == "on" else "普通图片"))
                decompose = str(p.get("decompose", "") or "").strip()
                if decompose:
                    lines.append("生图预处理：" + ("开启" if decompose == "on" else "关闭"))
                tag_prompt = str(p.get("tag_prompt", "") or "").strip()
                if tag_prompt:
                    lines.append(f"预处理用扩写：{tag_prompt}")
                return "\n".join(lines)
        return None

    def get_by_trigger(self, trigger: str) -> Optional[dict]:
        """按触发词返回完整的预设字典（含 model / params / 参考图等）。"""
        trigger = str(trigger or "").strip()
        if not trigger:
            return None
        for p in self.prompts:
            if p.get("enabled", True) is False:
                continue
            if self.get_trigger(p) == trigger:
                return p
        return None

    @staticmethod
    def _matchable(text: str) -> str:
        """把「触发词@某人」补成「触发词 @某人」，与 parser 的归一化保持一致。

        parser 内部会做这一步，但参考图匹配是另一个入口；不归一化的话
        `nd@某画师 一只猫` 会被 parser 认出来、却在这里匹配不上，
        预设里配的风格参考图就被静默丢掉了。
        """
        import re
        return re.sub(r"(?<=[^\s@])@", " @", str(text or ""))

    def get_preset_for_text(self, text: str) -> Optional[dict]:
        """按消息文本匹配预设（最长触发词优先），返回完整预设字典或 None。

        匹配规则与 parser.parse_prompt_message 一致：text 等于触发词或以“触发词+空格”开头。
        """
        text = self._matchable(text)
        if not text or not self.prompts:
            return None
        best = None
        best_len = -1
        for p in self.prompts:
            if p.get("enabled", True) is False:
                continue
            trigger = self.get_trigger(p)
            if not trigger:
                continue
            if text == trigger or text.startswith(trigger + " "):
                if len(trigger) > best_len:
                    best_len = len(trigger)
                    best = p
        return best

    def get_all(self) -> list[str]:
        """返回字符串列表，供 parser.parse_prompt_message 匹配使用。

        每项格式："trigger prompt_template"，与旧版完全兼容。
        """
        return [f"{p['trigger']} {p['prompt']}".strip() for p in self.prompts if p.get("enabled", True) is not False]

    def get_reference_image_for_text(self, text: str):
        """根据消息文本匹配预设，返回 (参考图原始配置值, 位置) 元组。

        匹配逻辑与 parser.parse_prompt_message 一致：最长触发词优先。
        参考图值可能是 list（file 上传类型）、str（旧版手动路径）或 None（未匹配/无参考图）。
        位置为 "before" 或 "after"。
        返回 None 表示未匹配到有参考图的预设。
        """
        if not text or not self.prompts:
            return None
        text = self._matchable(text)
        candidates = []
        for p in self.prompts:
            # 停用的预设不参与任何匹配：包括参考图注入，否则“停用”只停了一半
            if p.get("enabled", True) is False:
                continue
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
