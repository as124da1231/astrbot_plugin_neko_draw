# -*- coding: utf-8 -*-
"""白名单守卫。

支持群组 + 用户两级白名单，可在配置中静态维护，也可通过命令动态增删
（动态名单持久化到数据目录 whitelist.json）。
"""
import json
from pathlib import Path
from .storage import read_json, write_json


class WhitelistGuard:
    def __init__(
        self,
        enabled: bool,
        group_whitelist: list[str],
        user_whitelist: list[str],
        data_dir: str | Path,
        config=None,
    ):
        self.enabled = enabled
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.data_dir / "whitelist.json"
        self.config = config
        self._snapshot = self.data_dir / 'whitelist_config_snapshot.json'
        self.groups: set[str] = {str(x) for x in group_whitelist or []}
        self.users: set[str] = {str(x) for x in user_whitelist or []}
        current = {'groups': sorted(self.groups), 'users': sorted(self.users)}
        self._load()
        previous = read_json(self._snapshot, {})
        self.groups.difference_update(set(previous.get('groups', [])) - set(current['groups']))
        self.users.difference_update(set(previous.get('users', [])) - set(current['users']))
        write_json(self._snapshot, current)
        write_json(self._file, {'groups': sorted(self.groups), 'users': sorted(self.users)})

    def _load(self) -> None:
        if self._file.exists():
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
                self.groups.update(str(x) for x in data.get("groups", []))
                self.users.update(str(x) for x in data.get("users", []))
            except Exception:
                pass

    def _save(self) -> None:
        data = {'groups': sorted(self.groups), 'users': sorted(self.users)}
        write_json(self._file, data)
        if self.config is not None:
            self.config['group_whitelist'] = data['groups']
            self.config['user_whitelist'] = data['users']
            if hasattr(self.config, 'save_config'):
                self.config.save_config()
            write_json(self._snapshot, data)

    def check(self, user_id: str, group_id: str = "") -> bool:
        """通过返回 True；未启用白名单时放行。"""
        if not self.enabled:
            return True
        if str(user_id) in self.users:
            return True
        if group_id and str(group_id) in self.groups:
            return True
        return False

    def add(self, kind: str, target: str) -> tuple[bool, str]:
        kind = kind.strip()
        target = target.strip()
        if kind not in ("用户", "群组") or not target:
            return False, "用法：/nc wa <u|g> <ID>"
        if kind == "用户":
            self.users.add(target)
        else:
            self.groups.add(target)
        self._save()
        return True, f"已添加{kind}白名单：{target}"

    def delete(self, kind: str, target: str) -> tuple[bool, str]:
        kind = kind.strip()
        target = target.strip()
        if kind not in ("用户", "群组") or not target:
            return False, "用法：/nc wd <u|g> <ID>"
        if kind == "用户":
            if target in self.users:
                self.users.discard(target)
                self._save()
                return True, f"已删除{kind}白名单：{target}"
        else:
            if target in self.groups:
                self.groups.discard(target)
                self._save()
                return True, f"已删除{kind}白名单：{target}"
        return False, f"白名单中不存在：{target}"

    def list_whitelist(self) -> str:
        if not self.enabled:
            return "白名单未启用（所有用户可用）"
        return (
            f"白名单已启用\n群组({len(self.groups)})：{'、'.join(sorted(self.groups)) or '无'}\n"
            f"用户({len(self.users)})：{'、'.join(sorted(self.users)) or '无'}"
        )
