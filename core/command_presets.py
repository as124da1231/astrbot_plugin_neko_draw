# -*- coding: utf-8 -*-
"""扩写提示词 / 提取图片形象：两个互相独立的指令分区。

每个分区都有自己的触发词、提示词模板、系统提示词、超时和独立开关，
互不影响，也不与绘图预设（prompt）共享任何数据。

存储位置为数据目录下的独立 JSON 文件（expand_presets.json /
extract_presets.json），由 WebUI 页面与 /nc 指令共同维护；配置键
（expand_presets / extract_presets）只作为初始值与快照依据，
运行时始终与本地文件合并，保证面板与指令的修改都能保留。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .storage import read_json, write_json

# 分区定义：键名、文件、默认项
# 默认模板针对 Krea 2：吃自然语言长句，不堆 tag；2K 下画面小字需要双引号保真。
SECTIONS = {
    "expand": {
        "filename": "expand_presets.json",
        "snapshot": "expand_presets_snapshot.json",
        "label": "扩写提示词",
        "needs_image": False,
        "legacy_triggers": ["扩"],
        "defaults": [],
    },
    "extract": {
        "filename": "extract_presets.json",
        "snapshot": "extract_presets_snapshot.json",
        "label": "提取图片形象",
        "needs_image": True,
        "legacy_triggers": ["形象"],
        "defaults": [],
    },
}

# 旧版内置默认的系统提示词（作为识别「用户没改过」的指纹，升级时只替换这些）
LEGACY_DEFAULT_SYSTEM_PROMPTS = {"", "None"}

# 内置默认只补一次：升级后用户删掉的条目不会被重新塞回来
SEED_VERSION = 3


def _is_blank(value) -> bool:
    """判断“没填”：None / 空串 / 空列表 / 空字典。

    ``False`` 和 ``0`` 是有意义的值（停用、超时清零），不能当成空值，
    否则合并时会被旧值覆盖：典型表现是「停用指令保存后又变回启用」。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def section_spec(section: str) -> dict:
    key = str(section or "").strip().lower()
    if key not in SECTIONS:
        raise ValueError("未知的指令分区")
    return SECTIONS[key]


def forget_removed_triggers(data_dir, section: str, triggers) -> None:
    """把用户主动保存进配置的触发词移出“已删除”黑名单。

    指令删除会记黑名单，防止配置里的陈旧副本复活；但用户后来重新添加同一条
    指令时（新建、或从备份恢复），黑名单必须解除，否则会出现“加进去了却永远
    不生效”。保存接口是权威动作，调用它来解除。
    不依赖 store 实例：保存时运行组件可能还没重建。
    """
    names = {str(t).strip() for t in (triggers or []) if str(t).strip()}
    if not names:
        return
    path = Path(data_dir) / f"{section}_presets_removed.json"
    current = read_json(path, None)
    if not isinstance(current, list):
        return
    remaining = [str(x) for x in current if str(x) not in names]
    if len(remaining) == len(current):
        return
    try:
        write_json(path, remaining)
    except Exception:  # noqa: BLE001
        logger.debug("[neko_draw] 解除删除标记失败", exc_info=True)


def sync_removed_blacklist(data_dir, section: str, kept, deleted=None) -> None:
    """整表保存时的删除记账（WebUI 保存接口专用）。

    保存接口收到的是**完整列表**。本地 ``{section}_presets.json`` 里可能还留着用户
    刚删掉的那一条；加载时按并集合并会把它又并回来，用户看到的就是“点了删除、保存
    后还在”。所以这里必须以提交列表为准记一次账：

    - 本地/快照里有、但提交列表里没有的触发词 → 记为“已删除”（加载时压制）；
    - 提交列表里重新出现的触发词 → 解除“已删除”（支持删掉后再加回来）。

    只应在“提交里确实带了该分区的完整列表”时调用，否则会把没提交的分区整片误删。
    ``deleted`` 不为 None 时，表示调用方已经精确算出“这份页面加载时见过、现在没了”
    的触发词；此时只记这些，不再扫描本地文件（避免过期页面把没见过的条目也算成删除）。
    """
    kept_set = {str(t).strip() for t in (kept or []) if str(t).strip()}
    folder = Path(data_dir)
    removed_file = folder / f"{section}_presets_removed.json"
    current = read_json(removed_file, None)
    names = {str(x) for x in current} if isinstance(current, list) else set()
    # 提交列表里还在的，撤销删除标记
    names = {name for name in names if name not in kept_set}
    if deleted is not None:
        names |= {str(t).strip() for t in deleted if str(t).strip() and str(t).strip() not in kept_set}
    else:
        # 本地还留着、但这次没提交的，记为已删除
        for filename in (f"{section}_presets.json", f"{section}_presets_snapshot.json"):
            data = read_json(folder / filename, None)
            if not isinstance(data, list):
                continue
            for raw in data:
                norm = _normalize(raw)
                if norm and norm["trigger"] not in kept_set:
                    names.add(norm["trigger"])
    try:
        write_json(removed_file, sorted(names))
    except Exception:  # noqa: BLE001
        logger.debug("[neko_draw] 记录删除标记失败", exc_info=True)


_CJK_MIN = "\u2e80"  # CJK 部首扩展起点，覆盖中文、日文假名、全角标点之后的汉字区
_ATTACH_EXCLUDE = ("<", "@", "[", "【", "\\")


def _matches_trigger(text: str, trigger: str) -> bool:
    """判断消息是否以触发词开头。

    规则：
    - 完全相等、或后面跟空格 → 命中（原有行为）
    - 后面紧跟 @ 或非 ASCII 字符（全角冒号、中文、emoji 等）→ 命中，
      兼容「提取@某人」「形象：看发型」这类没有空格的写法
    - 紧跟英文字母/数字时不命中，避免「扩」误伤英文单词
    """
    if not text.startswith(trigger):
        return False
    rest = text[len(trigger):]
    if not rest:
        return True
    head = rest[0]
    if head.isspace():
        return True
    if head in ("@", "：", ":", "，", "、"):
        return True
    return ord(head) > 127 and head not in _ATTACH_EXCLUDE


def _normalize(raw) -> Optional[dict]:
    """把配置项归一化为 {trigger, prompt, system_prompt, timeout, model, enabled}。"""
    if isinstance(raw, str):
        raw = _string_to_dict(raw)
    if not isinstance(raw, dict):
        return None
    trigger = str(raw.get("trigger", "") or "").strip()
    if not trigger:
        return None
    try:
        timeout = int(raw.get("timeout", 0) or 0)
    except (TypeError, ValueError):
        timeout = 0
    item = {
        "trigger": trigger,
        "prompt": str(raw.get("prompt", "") or "").strip(),
        "system_prompt": str(raw.get("system_prompt", "") or "").strip(),
        "timeout": max(0, min(timeout, 600)),
        "model": str(raw.get("model", "") or "").strip(),
        "provider_id": str(raw.get("provider_id", "") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        # 独立会话三态：""=跟随该分区全局，on/off=本条强制
        "isolation": _isolation_choice(raw.get("isolation")),
    }
    template_key = raw.get("__template_key")
    if template_key:
        item["__template_key"] = template_key
    return item


def _isolation_choice(raw) -> str:
    """把独立会话设置归一化成三态；不认识的值一律当作「跟随全局」。"""
    value = str(raw or "").strip().lower()
    return value if value in ("on", "off") else ""


def _prefix_clash(triggers) -> Optional[tuple[str, str]]:
    """找出会互相抢的触发词对：短的那条永远不会被匹配到。

    消息用「最长触发词优先」匹配，所以「加」和「加长」这类前缀关系本身是正常的
    （用户打「加 猫」走短的那条，打「加长 猫」走长的那条）。只有更极端的情况才会
    让短的那条彻底失效：被停用的条目仍然参与匹配，或者同一分区里两条触发词完全一样。
    """
    seen: set[str] = set()
    for trigger in triggers:
        if trigger in seen:
            return trigger, trigger
        seen.add(trigger)
    return None


def _string_to_dict(text: str) -> dict:
    tokens = text.strip().split(None, 1)
    if not tokens:
        return {}
    return {"trigger": tokens[0], "prompt": tokens[1] if len(tokens) > 1 else ""}


class CommandPresetStore:
    """一个分区（扩写或提取）的预设集合。"""

    def __init__(self, section: str, config_items, data_dir: str | Path, config=None, config_key: str = ""):
        self.spec = section_spec(section)
        self.section = section
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.data_dir / self.spec["filename"]
        self._snapshot = self.data_dir / self.spec["snapshot"]
        self._seed_marker = self.data_dir / f"{section}_presets_seeded.json"
        self._removed_file = self.data_dir / f"{section}_presets_removed.json"
        self.config = config
        self.config_key = config_key or f"{section}_presets"
        self.already_seeded = False
        self.items = self._load(config_items)

    # ---------------- 加载与保存 ----------------
    def _load(self, config_items) -> list[dict]:
        """配置快照 + 本地文件合并，规则与绘图预设一致。"""
        webui: dict[str, dict] = {}
        if config_items:
            for raw in config_items:
                norm = _normalize(raw)
                if norm:
                    webui[norm["trigger"]] = norm

        local: dict[str, dict] = {}
        data = read_json(self._file, None)
        if isinstance(data, list):
            for raw in data:
                norm = _normalize(raw)
                if norm:
                    local[norm["trigger"]] = norm

        previous = read_json(self._snapshot, None)
        seeded = read_json(self._seed_marker, None)
        already_seeded = isinstance(seeded, dict) and seeded.get("version") == SEED_VERSION
        self.already_seeded = already_seeded

        # 统一的加载规则（3.15.2 起）：内容只由「配置快照 + 本地文件」决定。
        # 1. 两边都有 → 按字段合并，空值不覆盖已有内容（谁都不会整条丢掉）
        # 2. 只有一边有 → 保留（新增指令可能只写了一边，重启后又恰好先读另一边）
        # 3. 明确删除过的 → 永久排除，配置残留的旧副本不能复活
        # 不再自动补齐内置默认指令：删掉的条目不会自己长回来。
        merged: dict[str, dict] = dict(local)
        for trigger, incoming in webui.items():
            existing = merged.get(trigger)
            if existing is None:
                merged[trigger] = incoming
                continue
            combined = dict(incoming)
            for key, value in existing.items():
                # 只有“没填”才用本地旧值兜底。False / 0 是有意义的值：
                # 曾经用假值判断，导致「停用」保存后被本地文件里的 true 覆盖回去。
                if _is_blank(combined.get(key)):
                    combined[key] = value
            merged[trigger] = combined

        # 用户明确删除过的触发词，不因合并而复活。
        # 解除黑名单只发生在“用户主动保存配置”时（webui 保存接口会把提交列表里
        # 出现的触发词移出黑名单），这里不做猜测，避免陈旧配置把已删条目放回来。
        removed = read_json(self._removed_file, None)
        if isinstance(removed, list):
            for trigger in removed:
                merged.pop(str(trigger), None)

        if not merged and not already_seeded and not webui and not local:
            # 真正全新安装（配置和本地都空）：只用内置默认凑一份
            merged = {item["trigger"]: self._with_defaults(item) for item in self.spec["defaults"]}
        if not already_seeded:
            # 用真实的旧数据判断用户改没改过，而不是合并后的结果
            merged = self._seed_defaults(merged, source=dict(local, **webui))
        result = list(merged.values())
        try:
            write_json(self._file, result)
            write_json(self._snapshot, [item["trigger"] for item in result])
            if not already_seeded:
                write_json(self._seed_marker, {"version": SEED_VERSION})
        except Exception:
            pass
        # 本地有、配置没有的条目回写配置，免得配置文件里一直躺着“看不见的旧指令”
        current = self.config.get(self.config_key) if isinstance(self.config, dict) else None
        current_triggers = {
            _normalize(raw)["trigger"] for raw in (current or []) if _normalize(raw)
        } if isinstance(current, list) else set()
        if current_triggers != {item["trigger"] for item in result}:
            try:
                self.sync_config()
            except Exception:
                pass
        return result

    def _removed_file_for(self, section: str) -> Path:
        return Path(self.data_dir) / f"{section}_presets_removed.json"

    def forget_removed(self, section: str, triggers) -> None:
        """实例入口：转交给模块级实现（保存接口在存储未就绪时也能用）。"""
        forget_removed_triggers(self.data_dir, section, triggers)

    def sync_config(self) -> bool:
        """把当前条目写回插件配置快照（AstrBot 配置持久化）。

        只负责“尽量写成功”，不向调用方抛错：命令侧 /nc ea、/nc ed 已经改完本地
        文件，若这里因为配置对象不支持写盘而抛异常，用户会看到指令明明加上了却
        报错。真正的失败由部署环境日志体现。
        """
        if not isinstance(self.config, dict):
            return False
        self.config[self.config_key] = [
            dict(item, __template_key=item.get("__template_key") or f"{self.section}_item")
            for item in self.items
        ]
        save_config = getattr(self.config, "save_config", None)
        if not callable(save_config):
            return False
        try:
            save_config()
        except TypeError:
            # 少数实现要求带一个说明参数
            try:
                save_config(f"指令预设：{len(self.items)} 条")
            except Exception:  # noqa: BLE001
                logger.debug("[neko_draw] 写回指令配置失败", exc_info=True)
                return False
        except Exception:  # noqa: BLE001
            logger.debug("[neko_draw] 写回指令配置失败", exc_info=True)
            return False
        return True

    def _with_defaults(self, item: dict) -> dict:
        base = {"prompt": "", "system_prompt": "", "timeout": 0, "model": "", "enabled": True, "isolation": ""}
        base.update({k: v for k, v in item.items() if v not in (None, "")})
        return base

    def _seed_defaults(self, merged: dict, source: Optional[dict] = None) -> dict:
        """旧版默认值迁移（只在升级那一次执行）。

        3.15.2 起不再自动补齐内置默认指令：列表内容完全由用户保存的结果决定，
        只有本地文件为空的全新安装才用官方默认。这样用户删掉的、改过的条目
        都不会被内置默认顶回来。
        """
        if self.already_seeded or not merged:
            return merged
        defaults = self.spec.get("defaults") or []

        for item in defaults:
            trigger = item["trigger"]
            current = merged.get(trigger)
            if current is None:
                continue
            seed = self._with_defaults(item)
            written = (source or {}).get(trigger, current)
            if not str(written.get("system_prompt", "") or "").strip() and seed.get("system_prompt"):
                # 用户没写过系统提示词：用新版默认，保留触发词、模型、超时、启用状态
                current.update({
                    "prompt": seed.get("prompt", ""),
                    "system_prompt": seed["system_prompt"],
                })
        return merged

    def _save(self, sync_snapshot: bool = False) -> None:
        write_json(self._file, self.items)
        if sync_snapshot:
            # 触发词增删改后立刻刷新快照：下次加载才不会把这些条目当成旧残留丢掉
            try:
                write_json(self._snapshot, [item["trigger"] for item in self.items])
            except Exception:
                pass

    def _mark_seeded(self) -> None:
        """标记默认项已经处理过，后续加载不再补默认、不再迁移。"""
        try:
            write_json(self._seed_marker, {"version": SEED_VERSION})
        except Exception:
            pass

    # ---------------- 查询 ----------------
    def get_by_trigger(self, trigger: str) -> Optional[dict]:
        trigger = str(trigger or "").strip()
        if not trigger:
            return None
        for item in self.items:
            if item["trigger"] == trigger:
                return item
        return None

    def match(self, text: str) -> Optional[dict]:
        """按最长触发词匹配消息文本。

        除空格分隔外，还允许触发词直接紧贴非 ASCII 字符（如「提取@某人」）：
        AstrBot 解析 @ 时可能在触发词后不留空格，若不兼容会整条消息匹配不上。
        """
        from .text_utils import strip_leading_at

        # 去掉开头的 @ 提及：否则「@铃 提取」「@某人 提取」会静默失败
        text = strip_leading_at(str(text or "").strip())
        if not text:
            return None
        best, best_len = None, -1
        for item in self.items:
            trigger = item["trigger"]
            if not trigger:
                continue
            if not _matches_trigger(text, trigger):
                continue
            if len(trigger) > best_len:
                best, best_len = item, len(trigger)
        return best

    def triggers(self) -> list[str]:
        return [item["trigger"] for item in self.items]

    def list_items(self) -> list[dict]:
        return [dict(item) for item in self.items]

    def get_detail(self, trigger: str) -> Optional[str]:
        item = self.get_by_trigger(trigger)
        if item is None:
            return None
        lines = [f"{item['trigger']}（{self.spec['label']}）"]
        if item.get("enabled", True) is False:
            lines.append("状态：已停用")
        if item.get("model"):
            lines.append(f"模型：{item['model']}")
        if item.get("timeout"):
            lines.append(f"超时：{item['timeout']} 秒")
        lines.append("独立会话：" + {
            "on": "开启（仅这条）",
            "off": "关闭（仅这条）",
        }.get(str(item.get("isolation") or ""), "跟随全局"))
        if item.get("system_prompt"):
            lines.append(f"系统提示词：{item['system_prompt'][:60]}…")
        prompt = item.get("prompt", "")
        lines.append(f"提示词模板：{prompt[:120]}{'…' if len(prompt) > 120 else ''}")
        return "\n".join(lines)

    # ---------------- 维护（指令与 WebUI 共用） ----------------
    def upsert(self, item: dict) -> tuple[bool, str]:
        norm = _normalize(item)
        if norm is None:
            return False, "触发词不能为空"
        norm["__template_key"] = norm.get("__template_key") or f"{self.section}_item"
        if not norm.get("prompt"):
            norm["prompt"] = "{{user_text}}" if self.section == "expand" else self.spec["defaults"][0]["prompt"]
        others = {other["trigger"] for other in self.items if other["trigger"] != norm["trigger"]}
        clash = _prefix_clash(others | {norm["trigger"]})
        if clash:
            return False, (
                f"触发词「{clash[0]}」和「{clash[1]}」会互相抢："
                f"「{clash[0]}」开头的消息总是先匹配比较长的那条，短的那条永远用不上"
            )
        for index, existing in enumerate(self.items):
            if existing["trigger"] == norm["trigger"]:
                # 指令只改提示词时，保留面板里配置的系统提示词/模型/超时
                merged = dict(existing)
                merged.update({k: v for k, v in norm.items() if v not in (None, "")})
                merged["enabled"] = norm.get("enabled", existing.get("enabled", True))
                self.items[index] = merged
                self._save(sync_snapshot=True)
                # 命令侧改动也要同步进配置快照：否则网页下次保存会把旧列表写回来，
                # 用户会看到“用命令改完，网页一保存又变回去了”。
                self.sync_config()
                return True, f"已更新{self.spec['label']}：{norm['trigger']}"
        self.items.append(norm)
        self._save(sync_snapshot=True)
        self._mark_removed(norm["trigger"], False)
        self.sync_config()
        return True, f"已添加{self.spec['label']}：{norm['trigger']}"

    def _removed(self) -> set[str]:
        data = read_json(self._removed_file, None)
        return {str(x) for x in data} if isinstance(data, list) else set()

    def _mark_removed(self, trigger: str, removed: bool) -> None:
        """记录/取消“用户主动删除”，避免配置里的旧副本把删掉的条目复活。"""
        trigger = str(trigger or "").strip()
        if not trigger:
            return
        names = self._removed()
        if removed:
            names.add(trigger)
        else:
            names.discard(trigger)
        try:
            write_json(self._removed_file, sorted(names))
        except Exception:
            pass

    def _mark_removed_all(self, kept: set[str]) -> None:
        """整表保存后：面板里还在的条目撤销删除标记，其余历史上出现的触发词记为已删除。"""
        names = self._removed()
        new_names = {t for t in names if t not in kept}
        try:
            previous = read_json(self._snapshot, None)
            before = read_json(self._file, None)
        except Exception:
            previous, before = None, None
        for source in (previous, before):
            if isinstance(source, list):
                for raw in source:
                    norm = _normalize(raw)
                    if norm and norm["trigger"] not in kept:
                        new_names.add(norm["trigger"])
        try:
            write_json(self._removed_file, sorted(new_names))
        except Exception:
            pass

    def delete(self, trigger: str) -> tuple[bool, str]:
        trigger = str(trigger or "").strip()
        for index, item in enumerate(self.items):
            if item["trigger"] == trigger:
                self.items.pop(index)
                self._save(sync_snapshot=True)
                self._mark_seeded()
                self._mark_removed(trigger, True)
                # 同步配置快照：否则网页里那份旧列表会在下次保存时把这条指令带回来
                self.sync_config()
                return True, f"已删除{self.spec['label']}：{trigger}"
        return False, f"未找到触发词：{trigger}"

    def replace_all(self, items) -> list[dict]:
        """WebUI 整表保存：任意顺序，触发词唯一。"""
        if not isinstance(items, list):
            raise ValueError(f"{self.spec['label']}必须是列表")
        result: list[dict] = []
        seen: set[str] = set()
        for raw in items:
            norm = _normalize(raw)
            if norm is None:
                continue
            if norm["trigger"] in seen:
                raise ValueError(f"{self.spec['label']}触发词「{norm['trigger']}」重复")
            seen.add(norm["trigger"])
            norm["__template_key"] = norm.get("__template_key") or f"{self.section}_item"
            result.append(norm)
        clash = _prefix_clash(seen)
        if clash:
            raise ValueError(
                f"{self.spec['label']}触发词「{clash[0]}」和「{clash[1]}」会互相抢："
                f"「{clash[0]}」开头的消息总是先匹配比较长的那条，短的那条永远用不上"
            )
        self.items = result
        # 面板里不存在的旧触发词就是用户删掉的，记下来免得下次加载被配置里的旧副本复活
        self._mark_removed_all({item["trigger"] for item in self.items})
        self._save(sync_snapshot=True)
        self._mark_seeded()
        # 必须同步回配置快照：保存配置后运行时会按配置重建存储，
        # 只写本地文件会让刚保存的提示词被旧快照覆盖。
        self.sync_config()
        return self.list_items()

    def dump(self) -> str:
        return json.dumps(self.items, ensure_ascii=False, indent=2)
