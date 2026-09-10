# -*- coding: utf-8 -*-
"""APNG 制作记录：仅保留输入原图，成品始终为临时文件。"""
import sqlite3, threading, time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

_SCHEMA="""
CREATE TABLE IF NOT EXISTS apng_creations (id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp REAL NOT NULL,user_id TEXT,group_id TEXT,frame_count INTEGER NOT NULL,duration_ms INTEGER NOT NULL,loop INTEGER NOT NULL,file_path TEXT NOT NULL DEFAULT '',file_size INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS apng_assets (id INTEGER PRIMARY KEY AUTOINCREMENT,record_id INTEGER NOT NULL,position INTEGER NOT NULL,file_path TEXT NOT NULL,thumbnail_path TEXT DEFAULT '',UNIQUE(record_id,position));
CREATE INDEX IF NOT EXISTS idx_apng_timestamp ON apng_creations(timestamp);
CREATE INDEX IF NOT EXISTS idx_apng_assets_record ON apng_assets(record_id);
"""
class ApngHistoryStore:
    def __init__(self,data_dir:str|Path):
        self.data_dir=Path(data_dir); self.data_dir.mkdir(parents=True,exist_ok=True); self._db_path=self.data_dir/"apng_history.db"; self._lock=threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # 旧版成品不再保留；只清除插件自己的 APNG 文件与路径引用。
            rows=conn.execute("SELECT id,file_path FROM apng_creations WHERE file_path<>''").fetchall()
            for row in rows: self._safe_delete(row["file_path"],"apng")
            conn.execute("UPDATE apng_creations SET file_path='' WHERE file_path<>''")
    @contextmanager
    def _connect(self):
        conn=sqlite3.connect(str(self._db_path),check_same_thread=False); conn.row_factory=sqlite3.Row
        try:
            with conn: yield conn
        finally: conn.close()
    def add(self,*,user_id,group_id,frame_count,duration_ms,loop,file_path="",file_size=None,source_image_paths=None,source_thumbnail_paths=None)->int:
        paths,thumbs=list(source_image_paths or []),list(source_thumbnail_paths or [])
        size=int(file_size if file_size is not None else (Path(file_path).stat().st_size if file_path and Path(file_path).is_file() else 0))
        if file_path: self._safe_delete(file_path,"apng")
        with self._lock,self._connect() as conn:
            cur=conn.execute("INSERT INTO apng_creations(timestamp,user_id,group_id,frame_count,duration_ms,loop,file_path,file_size) VALUES(?,?,?,?,?,?,?,?)",(time.time(),str(user_id),str(group_id),int(frame_count),int(duration_ms),int(loop),"",size)); rid=int(cur.lastrowid)
            for pos,path in enumerate(paths): conn.execute("INSERT INTO apng_assets(record_id,position,file_path,thumbnail_path) VALUES(?,?,?,?)",(rid,pos,str(path),str(thumbs[pos] if pos<len(thumbs) else "")))
            return rid
    def _hydrate(self,conn,row):
        item=dict(row); assets=conn.execute("SELECT file_path,thumbnail_path FROM apng_assets WHERE record_id=? ORDER BY position",(item["id"],)).fetchall(); item["source_image_paths"]=[a["file_path"] for a in assets]; item["source_thumbnail_paths"]=[a["thumbnail_path"] or "" for a in assets]; item["source_count"]=len(assets); item["file_path"]=""; return item
    def list(self,page=1,page_size=20)->dict[str,Any]:
        page,page_size=max(1,int(page)),max(1,min(100,int(page_size)))
        with self._lock,self._connect() as conn:
            total=conn.execute("SELECT COUNT(*) FROM apng_creations").fetchone()[0]; rows=conn.execute("SELECT * FROM apng_creations ORDER BY timestamp DESC LIMIT ? OFFSET ?",(page_size,(page-1)*page_size)).fetchall(); items=[self._hydrate(conn,r) for r in rows]
        return {"items":items,"total":total,"page":page,"page_size":page_size}
    def get(self,record_id:int)->Optional[dict]:
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT * FROM apng_creations WHERE id=?",(int(record_id),)).fetchone(); return self._hydrate(conn,row) if row else None
    def _safe_delete(self,value,kind="asset"):
        if not value:return
        try:
            path=Path(value).resolve(); roots={(self.data_dir/"save_images").resolve():("apng_maker_",),(self.data_dir/"history_inputs").resolve():("apng_source_",),(self.data_dir/"history_thumbnails").resolve():("thumb_",)}; prefixes=roots.get(path.parent)
            if prefixes and path.name.startswith(prefixes): path.unlink(missing_ok=True)
        except (OSError,ValueError): pass
    def _delete_many(self,ids):
        if not ids:return 0
        marks=",".join("?" for _ in ids)
        with self._lock,self._connect() as conn:
            assets=conn.execute(f"SELECT file_path,thumbnail_path FROM apng_assets WHERE record_id IN ({marks})",ids).fetchall(); legacy=conn.execute(f"SELECT file_path FROM apng_creations WHERE id IN ({marks})",ids).fetchall(); conn.execute(f"DELETE FROM apng_assets WHERE record_id IN ({marks})",ids); cur=conn.execute(f"DELETE FROM apng_creations WHERE id IN ({marks})",ids)
        for value in set([v for r in assets for v in (r["file_path"],r["thumbnail_path"])]+[r["file_path"] for r in legacy]): self._safe_delete(value)
        return int(cur.rowcount)
    def delete(self,record_id): return self._delete_many([int(record_id)])>0
    def clear(self):
        with self._lock,self._connect() as conn: ids=[r[0] for r in conn.execute("SELECT id FROM apng_creations").fetchall()]
        return self._delete_many(ids)
    def enforce_limit(self,limit):
        limit=max(0,int(limit))
        with self._lock,self._connect() as conn: ids=[r[0] for r in conn.execute("SELECT id FROM apng_creations ORDER BY timestamp DESC,id DESC LIMIT -1 OFFSET ?",(limit,)).fetchall()]
        return self._delete_many(ids)
