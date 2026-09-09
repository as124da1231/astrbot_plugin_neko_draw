# -*- coding: utf-8 -*-
"""独立的 APNG 制作记录与文件生命周期管理。"""
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS apng_creations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    user_id TEXT,
    group_id TEXT,
    frame_count INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    loop INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_apng_timestamp ON apng_creations(timestamp);
"""


class ApngHistoryStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.data_dir / "apng_history.db"
        self._lock = threading.Lock()
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

    def add(self, *, user_id: str, group_id: str, frame_count: int,
            duration_ms: int, loop: int, file_path: str) -> int:
        path = Path(file_path)
        size = path.stat().st_size if path.is_file() else 0
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO apng_creations
                   (timestamp,user_id,group_id,frame_count,duration_ms,loop,file_path,file_size)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (time.time(), str(user_id), str(group_id), int(frame_count),
                 int(duration_ms), int(loop), str(path), size),
            )
            return int(cur.lastrowid)

    def list(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        with self._lock, self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM apng_creations").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM apng_creations ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "page": page, "page_size": page_size}

    def get(self, record_id: int) -> Optional[dict]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM apng_creations WHERE id = ?", (int(record_id),)).fetchone()
        return dict(row) if row else None

    def _safe_delete_file(self, file_path: str) -> None:
        path = Path(file_path)
        try:
            resolved = path.resolve()
            save_root = (self.data_dir / "save_images").resolve()
            if resolved.is_relative_to(save_root) and resolved.name.startswith("apng_maker_"):
                resolved.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def delete(self, record_id: int) -> bool:
        item = self.get(record_id)
        if item is None:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM apng_creations WHERE id = ?", (int(record_id),))
        if cur.rowcount:
            self._safe_delete_file(item["file_path"])
            return True
        return False

    def clear(self) -> int:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT file_path FROM apng_creations").fetchall()
            cur = conn.execute("DELETE FROM apng_creations")
        for row in rows:
            self._safe_delete_file(row["file_path"])
        return int(cur.rowcount)
