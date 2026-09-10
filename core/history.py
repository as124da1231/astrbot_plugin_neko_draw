# -*- coding: utf-8 -*-
"""生成历史与插件托管图片的统一生命周期。"""
from __future__ import annotations
import json, sqlite3, threading, time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp REAL NOT NULL,user_id TEXT,group_id TEXT,trigger_word TEXT,prompt TEXT,params TEXT,model_template TEXT,provider TEXT,model TEXT,refer_image_count INTEGER DEFAULT 0,status TEXT NOT NULL,error_message TEXT,image_paths TEXT,source_image_paths TEXT,generation_time_ms REAL);
CREATE TABLE IF NOT EXISTS generation_assets (id INTEGER PRIMARY KEY AUTOINCREMENT,generation_id INTEGER NOT NULL,kind TEXT NOT NULL,position INTEGER NOT NULL,file_path TEXT NOT NULL,thumbnail_path TEXT DEFAULT '',UNIQUE(generation_id,kind,position));
CREATE INDEX IF NOT EXISTS idx_generations_timestamp ON generations(timestamp);
CREATE INDEX IF NOT EXISTS idx_generations_user ON generations(user_id);
CREATE INDEX IF NOT EXISTS idx_generations_status ON generations(status);
CREATE INDEX IF NOT EXISTS idx_generations_model ON generations(model_template);
CREATE INDEX IF NOT EXISTS idx_generation_assets_record ON generation_assets(generation_id);
"""

class HistoryStore:
    def __init__(self, data_dir: str | Path):
        self.data_dir=Path(data_dir); self.data_dir.mkdir(parents=True,exist_ok=True)
        self._db_path=self.data_dir/"history.db"; self._lock=threading.Lock(); self._init_db()
    @contextmanager
    def _connect(self):
        conn=sqlite3.connect(str(self._db_path),check_same_thread=False); conn.row_factory=sqlite3.Row
        try:
            with conn: yield conn
        finally: conn.close()
    @staticmethod
    def _loads(value,fallback):
        try: return json.loads(value or "")
        except (json.JSONDecodeError,TypeError): return fallback
    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            cols={r[1] for r in conn.execute("PRAGMA table_info(generations)")}
            if "source_image_paths" not in cols: conn.execute("ALTER TABLE generations ADD COLUMN source_image_paths TEXT")
            for row in conn.execute("SELECT id,image_paths,source_image_paths FROM generations").fetchall():
                if conn.execute("SELECT 1 FROM generation_assets WHERE generation_id=? LIMIT 1",(row["id"],)).fetchone(): continue
                for kind,field in (("output","image_paths"),("input","source_image_paths")):
                    for pos,path in enumerate(self._loads(row[field],[])):
                        conn.execute("INSERT OR IGNORE INTO generation_assets(generation_id,kind,position,file_path) VALUES(?,?,?,?)",(row["id"],kind,pos,str(path)))
    def add(self,*,user_id="",group_id="",trigger_word="",prompt="",params=None,model_template="",provider="",model="",refer_image_count=0,status="success",error_message="",image_paths=None,source_image_paths=None,image_thumbnail_paths=None,source_thumbnail_paths=None,generation_time_ms=0.0)->int:
        outputs,inputs=list(image_paths or []),list(source_image_paths or [])
        ots,its=list(image_thumbnail_paths or []),list(source_thumbnail_paths or [])
        with self._lock,self._connect() as conn:
            cur=conn.execute("INSERT INTO generations(timestamp,user_id,group_id,trigger_word,prompt,params,model_template,provider,model,refer_image_count,status,error_message,image_paths,source_image_paths,generation_time_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(time.time(),user_id,group_id,trigger_word,prompt,json.dumps(params or {},ensure_ascii=False),model_template,provider,model,refer_image_count,status,error_message,json.dumps(outputs,ensure_ascii=False),json.dumps(inputs,ensure_ascii=False),generation_time_ms))
            rid=int(cur.lastrowid)
            for kind,paths,thumbs in (("output",outputs,ots),("input",inputs,its)):
                for pos,path in enumerate(paths): conn.execute("INSERT INTO generation_assets(generation_id,kind,position,file_path,thumbnail_path) VALUES(?,?,?,?,?)",(rid,kind,pos,str(path),str(thumbs[pos] if pos<len(thumbs) else "")))
            return rid
    def _hydrate(self,conn,row):
        item=dict(row); item["params"]=self._loads(item.get("params"),{})
        assets=conn.execute("SELECT kind,position,file_path,thumbnail_path FROM generation_assets WHERE generation_id=? ORDER BY kind,position",(item["id"],)).fetchall()
        if assets:
            for kind,pk,tk in (("output","image_paths","image_thumbnail_paths"),("input","source_image_paths","source_thumbnail_paths")):
                selected=[a for a in assets if a["kind"]==kind]; item[pk]=[a["file_path"] for a in selected]; item[tk]=[a["thumbnail_path"] or "" for a in selected]
        else:
            item["image_paths"]=self._loads(item.get("image_paths"),[]); item["source_image_paths"]=self._loads(item.get("source_image_paths"),[]); item["image_thumbnail_paths"]=[]; item["source_thumbnail_paths"]=[]
        return item
    def list(self,*,page=1,page_size=20,user_id="",model_template="",status="",start_time=0.0,end_time=0.0)->dict[str,Any]:
        page,page_size=max(1,int(page)),max(1,min(100,int(page_size))); cond=[]; vals=[]
        for col,val in (("user_id",user_id),("model_template",model_template),("status",status)):
            if val: cond.append(f"{col} = ?"); vals.append(val)
        if start_time>0: cond.append("timestamp >= ?"); vals.append(start_time)
        if end_time>0: cond.append("timestamp <= ?"); vals.append(end_time)
        where=" WHERE "+" AND ".join(cond) if cond else ""
        with self._lock,self._connect() as conn:
            total=conn.execute(f"SELECT COUNT(*) FROM generations{where}",vals).fetchone()[0]
            rows=conn.execute(f"SELECT * FROM generations{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",vals+[page_size,(page-1)*page_size]).fetchall(); items=[self._hydrate(conn,r) for r in rows]
        return {"items":items,"total":total,"page":page,"page_size":page_size}
    def get(self,record_id:int)->Optional[dict]:
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT * FROM generations WHERE id=?",(int(record_id),)).fetchone(); return self._hydrate(conn,row) if row else None
    def _safe_delete(self,value):
        if not value:return
        try:
            path=Path(value).resolve(); rules={(self.data_dir/"save_images").resolve():("img_",),(self.data_dir/"history_inputs").resolve():("source_",),(self.data_dir/"history_thumbnails").resolve():("thumb_",)}; prefixes=rules.get(path.parent)
            if prefixes and path.name.startswith(prefixes): path.unlink(missing_ok=True)
        except (OSError,ValueError): pass
    def _delete_source_files(self, paths: list[str]) -> None:
        """兼容旧调用：只删除插件托管的输入副本。"""
        for path in paths: self._safe_delete(path)
    def _delete_many(self,ids):
        if not ids:return 0
        marks=",".join("?" for _ in ids)
        with self._lock,self._connect() as conn:
            assets=conn.execute(f"SELECT file_path,thumbnail_path FROM generation_assets WHERE generation_id IN ({marks})",ids).fetchall(); legacy=conn.execute(f"SELECT image_paths,source_image_paths FROM generations WHERE id IN ({marks})",ids).fetchall(); conn.execute(f"DELETE FROM generation_assets WHERE generation_id IN ({marks})",ids); cur=conn.execute(f"DELETE FROM generations WHERE id IN ({marks})",ids)
        paths=[v for r in assets for v in (r["file_path"],r["thumbnail_path"])]
        for r in legacy: paths+=self._loads(r["image_paths"],[])+self._loads(r["source_image_paths"],[])
        for path in set(paths): self._safe_delete(path)
        return int(cur.rowcount)
    def delete(self,record_id): return self._delete_many([int(record_id)])>0
    def clear(self):
        with self._lock,self._connect() as conn: ids=[r[0] for r in conn.execute("SELECT id FROM generations").fetchall()]
        return self._delete_many(ids)
    def enforce_limit(self,limit):
        limit=max(0,int(limit))
        with self._lock,self._connect() as conn: ids=[r[0] for r in conn.execute("SELECT id FROM generations ORDER BY timestamp DESC,id DESC LIMIT -1 OFFSET ?",(limit,)).fetchall()]
        return self._delete_many(ids)
    def stats(self):
        with self._lock,self._connect() as conn:
            total=conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]; success=conn.execute("SELECT COUNT(*) FROM generations WHERE status='success'").fetchone()[0]; failed=conn.execute("SELECT COUNT(*) FROM generations WHERE status='failed'").fetchone()[0]; models=conn.execute("SELECT model_template,COUNT(*) cnt FROM generations GROUP BY model_template ORDER BY cnt DESC").fetchall()
        return {"total":total,"success":success,"failed":failed,"by_model":[{"model":r["model_template"],"count":r["cnt"]} for r in models]}
