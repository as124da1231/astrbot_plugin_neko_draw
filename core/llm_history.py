# -*- coding: utf-8 -*-
"""扩写 / 提取指令的调用历史。

与绘图历史完全分开存储（llm_history.db），只记录「对话模型调用」本身的
输入输出、使用的提供商与模型、耗时、耗时档位与失败原因，不含任何图片。

表结构与绘图历史保持一致的结构风格：单表 + 索引，字段全部为可读文本，
方便在 WebUI 里直接展示与筛选。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    section TEXT NOT NULL,
    trigger_word TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    group_id TEXT DEFAULT '',
    umo TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    model TEXT DEFAULT '',
    timeout_total REAL DEFAULT 0,
    duration_ms REAL DEFAULT 0,
    status TEXT NOT NULL,
    error_message TEXT DEFAULT '',
    input_text TEXT DEFAULT '',
    system_prompt TEXT DEFAULT '',
    prompt TEXT DEFAULT '',
    output_text TEXT DEFAULT '',
    image_count INTEGER DEFAULT 0,
    image_source TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_timestamp ON llm_calls(timestamp);
CREATE INDEX IF NOT EXISTS idx_llm_calls_section ON llm_calls(section);
CREATE INDEX IF NOT EXISTS idx_llm_calls_status ON llm_calls(status);
"""

# 加在这个索引依赖的列补好之后才能建：老库里没有 source 列，
# 若把建索引写进上面的 _SCHEMA，executescript 会整段失败，插件直接起不来。
_POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_llm_calls_source ON llm_calls(source);
"""

# 列表页不返回大字段（输入输出正文），详情页再单独取，避免一页拖几百 KB
_LIST_COLUMNS = (
    "id,timestamp,section,trigger_word,user_id,group_id,provider,model,"
    "duration_ms,status,error_message,image_count,image_source,source"
)

# 旧库补列：SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，用 PRAGMA 先探测
_MIGRATIONS = (
    ("source", "ALTER TABLE llm_calls ADD COLUMN source TEXT DEFAULT 'user'"),
)

# 调用来源：用户主动发送的指令 / 生图流程自动预处理
SOURCE_USER = "user"
SOURCE_BOT = "bot"


class LlmHistoryStore:
    """扩写 / 提取的对话模型调用记录。"""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.data_dir / "llm_history.db"
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            existing = {
                row["name"] for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()
            }
            for column, statement in _MIGRATIONS:
                if column in existing:
                    continue
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    # 并发初始化时另一个连接已经加过这一列
                    pass
            # 索引依赖的列这时才齐，放在补列之后建
            conn.executescript(_POST_MIGRATION_SCHEMA)

    # ---------------- 写入 ----------------
    def add(
        self,
        *,
        section: str,
        trigger_word: str = "",
        user_id: str = "",
        group_id: str = "",
        umo: str = "",
        provider: str = "",
        model: str = "",
        timeout_total: float = 0.0,
        duration_ms: float = 0.0,
        status: str = "success",
        error_message: str = "",
        input_text: str = "",
        system_prompt: str = "",
        prompt: str = "",
        output_text: str = "",
        image_count: int = 0,
        image_source: str = "",
        source: str = SOURCE_USER,
    ) -> int:
        def text(value) -> str:
            return value if isinstance(value, str) else ("" if value is None else str(value))

        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO llm_calls(timestamp,section,trigger_word,user_id,group_id,umo,"
                "provider,model,timeout_total,duration_ms,status,error_message,input_text,"
                "system_prompt,prompt,output_text,image_count,image_source,source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(), text(section), text(trigger_word), text(user_id),
                    text(group_id), text(umo), text(provider), text(model),
                    float(timeout_total or 0), float(duration_ms or 0), text(status) or "success",
                    text(error_message), text(input_text), text(system_prompt), text(prompt),
                    text(output_text), int(image_count or 0), text(image_source),
                    text(source) or SOURCE_USER,
                ),
            )
            return int(cur.lastrowid)

    # ---------------- 读取 ----------------
    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        section: str = "",
        status: str = "",
        source: str = "",
        start_time: float = 0.0,
        end_time: float = 0.0,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        cond: list[str] = []
        vals: list[Any] = []
        if section in ("expand", "extract"):
            cond.append("section = ?")
            vals.append(section)
        if status:
            cond.append("status = ?")
            vals.append(status)
        if source in (SOURCE_USER, SOURCE_BOT):
            cond.append("source = ?")
            vals.append(source)
        if start_time > 0:
            cond.append("timestamp >= ?")
            vals.append(float(start_time))
        if end_time > 0:
            cond.append("timestamp <= ?")
            vals.append(float(end_time))
        where = " WHERE " + " AND ".join(cond) if cond else ""
        with self._lock, self._connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM llm_calls{where}", vals).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM llm_calls{where} "
                "ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?",
                vals + [page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [dict(r) for r in rows],
            "total": int(total),
            "page": page,
            "page_size": page_size,
        }

    def get(self, record_id: int) -> Optional[dict]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM llm_calls WHERE id=?", (int(record_id),)).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict[str, Any]:
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
            success = conn.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE status='success'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE status='failed'"
            ).fetchone()[0]
            avg = conn.execute("SELECT AVG(duration_ms) FROM llm_calls").fetchone()[0] or 0
            by_section = conn.execute(
                "SELECT section, COUNT(*) cnt FROM llm_calls GROUP BY section"
            ).fetchall()
            by_model = conn.execute(
                "SELECT COALESCE(NULLIF(model,''), provider) name, COUNT(*) cnt FROM llm_calls "
                "GROUP BY name ORDER BY cnt DESC LIMIT 10"
            ).fetchall()
        return {
            "total": int(total),
            "success": int(success),
            "failed": int(failed),
            "avg_ms": round(float(avg), 1),
            "by_section": [{"section": r["section"], "count": r["cnt"]} for r in by_section],
            "by_model": [{"name": r["name"], "count": r["cnt"]} for r in by_model],
        }

    # ---------------- 维护 ----------------
    def delete(self, record_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM llm_calls WHERE id=?", (int(record_id),))
            return bool(cur.rowcount)

    def clear(self) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM llm_calls")
            return int(cur.rowcount)

    def enforce_limit(self, limit: int) -> int:
        """只保留最近 limit 条；limit<=0 表示不限制。"""
        limit = int(limit or 0)
        if limit <= 0:
            return 0
        deleted = 0
        while True:
            with self._lock, self._connect() as conn:
                ids = [
                    r[0] for r in conn.execute(
                        "SELECT id FROM llm_calls ORDER BY timestamp DESC, id DESC LIMIT 500 OFFSET ?",
                        (limit,),
                    ).fetchall()
                ]
            if not ids:
                return deleted
            # 分批删除：SQLite 的 SQL 变量数量有上限（新版本约 32766），
            # 一次塞太多会在超限时抛错，而调用方会吞掉异常，结果就是“上限永远不生效”。
            with self._lock, self._connect() as conn:
                for start in range(0, len(ids), 400):
                    chunk = ids[start:start + 400]
                    marks = ",".join("?" for _ in chunk)
                    cur = conn.execute(f"DELETE FROM llm_calls WHERE id IN ({marks})", chunk)
                    deleted += int(cur.rowcount or 0)
