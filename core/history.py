# -*- coding: utf-8 -*-
"""生成历史记录（SQLite 持久化）。

每次绘图触发后记录：时间、用户、群组、触发词、提示词、参数、
模型模板、提供商、参考图数量、状态、错误信息、图片路径、耗时。
支持按用户/模型/状态/时间范围筛选，分页查询。
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    user_id TEXT,
    group_id TEXT,
    trigger_word TEXT,
    prompt TEXT,
    params TEXT,
    model_template TEXT,
    provider TEXT,
    model TEXT,
    refer_image_count INTEGER DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT,
    image_paths TEXT,
    generation_time_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_generations_timestamp ON generations(timestamp);
CREATE INDEX IF NOT EXISTS idx_generations_user ON generations(user_id);
CREATE INDEX IF NOT EXISTS idx_generations_status ON generations(status);
CREATE INDEX IF NOT EXISTS idx_generations_model ON generations(model_template);
"""


class HistoryStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.data_dir / "history.db"
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def add(
        self,
        *,
        user_id: str = "",
        group_id: str = "",
        trigger_word: str = "",
        prompt: str = "",
        params: Optional[dict] = None,
        model_template: str = "",
        provider: str = "",
        model: str = "",
        refer_image_count: int = 0,
        status: str = "success",
        error_message: str = "",
        image_paths: Optional[list[str]] = None,
        generation_time_ms: float = 0.0,
    ) -> int:
        """插入一条生成记录，返回记录 ID。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO generations
                   (timestamp, user_id, group_id, trigger_word, prompt, params,
                    model_template, provider, model, refer_image_count,
                    status, error_message, image_paths, generation_time_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    time.time(),
                    user_id,
                    group_id,
                    trigger_word,
                    prompt,
                    json.dumps(params or {}, ensure_ascii=False),
                    model_template,
                    provider,
                    model,
                    refer_image_count,
                    status,
                    error_message,
                    json.dumps(image_paths or [], ensure_ascii=False),
                    generation_time_ms,
                ),
            )
            return cur.lastrowid

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        user_id: str = "",
        model_template: str = "",
        status: str = "",
        start_time: float = 0.0,
        end_time: float = 0.0,
    ) -> dict[str, Any]:
        """分页查询历史记录。

        返回 {"items": [...], "total": int, "page": int, "page_size": int}
        """
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        offset = (page - 1) * page_size

        conditions = []
        params_list: list[Any] = []
        if user_id:
            conditions.append("user_id = ?")
            params_list.append(user_id)
        if model_template:
            conditions.append("model_template = ?")
            params_list.append(model_template)
        if status:
            conditions.append("status = ?")
            params_list.append(status)
        if start_time > 0:
            conditions.append("timestamp >= ?")
            params_list.append(start_time)
        if end_time > 0:
            conditions.append("timestamp <= ?")
            params_list.append(end_time)

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

        with self._lock, self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM generations{where}", params_list
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT * FROM generations{where}
                    ORDER BY timestamp DESC LIMIT ? OFFSET ?""",
                params_list + [page_size, offset],
            ).fetchall()

        items = []
        for row in rows:
            item = dict(row)
            item["params"] = json.loads(item.get("params") or "{}")
            item["image_paths"] = json.loads(item.get("image_paths") or "[]")
            items.append(item)

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get(self, record_id: int) -> Optional[dict]:
        """获取单条记录详情。"""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM generations WHERE id = ?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["params"] = json.loads(item.get("params") or "{}")
        item["image_paths"] = json.loads(item.get("image_paths") or "[]")
        return item

    def delete(self, record_id: int) -> bool:
        """删除一条记录（不删除图片文件）。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM generations WHERE id = ?", (record_id,))
            return cur.rowcount > 0

    def clear(self) -> int:
        """清空所有历史记录，返回删除条数。"""
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM generations")
            return cur.rowcount

    def stats(self) -> dict[str, Any]:
        """统计概览：总次数、成功数、失败数、各模型使用次数。"""
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
            success = conn.execute(
                "SELECT COUNT(*) FROM generations WHERE status = 'success'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM generations WHERE status = 'failed'"
            ).fetchone()[0]
            model_rows = conn.execute(
                """SELECT model_template, COUNT(*) as cnt
                   FROM generations GROUP BY model_template
                   ORDER BY cnt DESC"""
            ).fetchall()
        return {
            "total": total,
            "success": success,
            "failed": failed,
            "by_model": [{"model": r["model_template"], "count": r["cnt"]} for r in model_rows],
        }
