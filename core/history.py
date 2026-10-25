# -*- coding: utf-8 -*-
"""生成历史与插件托管图片的统一生命周期。"""
from __future__ import annotations
import json, sqlite3, threading, time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (id INTEGER PRIMARY KEY AUTOINCREMENT,timestamp REAL NOT NULL,user_id TEXT,group_id TEXT,trigger_word TEXT,prompt TEXT,params TEXT,model_template TEXT,provider TEXT,model TEXT,refer_image_count INTEGER DEFAULT 0,status TEXT NOT NULL,error_message TEXT,image_paths TEXT,source_image_paths TEXT,task_id TEXT,umo TEXT,generation_time_ms REAL);
CREATE TABLE IF NOT EXISTS generation_assets (id INTEGER PRIMARY KEY AUTOINCREMENT,generation_id INTEGER NOT NULL,kind TEXT NOT NULL,position INTEGER NOT NULL,file_path TEXT NOT NULL,thumbnail_path TEXT DEFAULT '',UNIQUE(generation_id,kind,position));
CREATE INDEX IF NOT EXISTS idx_generations_timestamp ON generations(timestamp);
CREATE INDEX IF NOT EXISTS idx_generations_user ON generations(user_id);
CREATE INDEX IF NOT EXISTS idx_generations_status ON generations(status);
CREATE INDEX IF NOT EXISTS idx_generations_model ON generations(model_template);
CREATE INDEX IF NOT EXISTS idx_generation_assets_record ON generation_assets(generation_id);
"""

# 关键词搜索会扫这些列：提示词（含实际发给模型的最终提示词）、触发词、模型、
# 模板名、任务 ID、用户 ID、失败原因。模糊匹配够用，也不依赖分词。
_KEYWORD_COLUMNS = (
    "prompt", "resolved_prompt", "trigger_word", "model_template",
    "model", "task_id", "user_id", "error_message",
)
# 排序白名单：用户传什么都不会被直接拼进 SQL
_ORDERS = {
    "time_desc": "timestamp DESC, id DESC",
    "time_asc": "timestamp ASC, id ASC",
    "duration_desc": "COALESCE(generation_time_ms, 0) DESC, timestamp DESC, id DESC",
    "duration_asc": "COALESCE(generation_time_ms, 0) ASC, timestamp DESC, id DESC",
}


def _like_pattern(term: str) -> str:
    """把关键词转成 LIKE 模式：转义 % _ 与反斜杠，避免输入被当成通配模式。"""
    escaped = str(term).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
            if "task_id" not in cols: conn.execute("ALTER TABLE generations ADD COLUMN task_id TEXT")
            if "umo" not in cols: conn.execute("ALTER TABLE generations ADD COLUMN umo TEXT")
            # 实际发给模型的提示词（含 LLM 拆解结果）与本次生效的选项快照
            if "resolved_prompt" not in cols: conn.execute("ALTER TABLE generations ADD COLUMN resolved_prompt TEXT DEFAULT ''")
            if "options" not in cols: conn.execute("ALTER TABLE generations ADD COLUMN options TEXT DEFAULT ''")
            # 不为 NULL，否则老库里“排队中”会被误判成已完成
            if "pending" not in cols: conn.execute("ALTER TABLE generations ADD COLUMN pending INTEGER DEFAULT 0")
            for row in conn.execute("SELECT id,image_paths,source_image_paths FROM generations").fetchall():
                if conn.execute("SELECT 1 FROM generation_assets WHERE generation_id=? LIMIT 1",(row["id"],)).fetchone(): continue
                for kind,field in (("output","image_paths"),("input","source_image_paths")):
                    for pos,path in enumerate(self._loads(row[field],[])):
                        conn.execute("INSERT OR IGNORE INTO generation_assets(generation_id,kind,position,file_path) VALUES(?,?,?,?)",(row["id"],kind,pos,str(path)))
    def add(self,*,user_id="",group_id="",trigger_word="",prompt="",params=None,model_template="",provider="",model="",refer_image_count=0,status="success",error_message="",image_paths=None,source_image_paths=None,image_thumbnail_paths=None,source_thumbnail_paths=None,task_id="",umo="",generation_time_ms=0.0,resolved_prompt="",options=None,pending=False)->int:
        outputs,inputs=list(image_paths or []),list(source_image_paths or [])
        ots,its=list(image_thumbnail_paths or []),list(source_thumbnail_paths or [])
        # SQLite 只接受 str/int/float/None/bytes；任何一个字段混进其它对象都会
        # 让整条历史写入失败（生成图还得被清理掉）。统一兜底成字符串更稳。
        def _text(value):
            return value if isinstance(value, (str, int, float)) or value is None else str(value)
        model_template,provider,model=_text(model_template),_text(provider),_text(model)
        trigger_word,error_message=_text(trigger_word),_text(error_message)
        with self._lock,self._connect() as conn:
            cur=conn.execute("INSERT INTO generations(timestamp,user_id,group_id,trigger_word,prompt,params,model_template,provider,model,refer_image_count,status,error_message,image_paths,source_image_paths,task_id,umo,generation_time_ms,resolved_prompt,options,pending) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(time.time(),user_id,group_id,trigger_word,prompt,json.dumps(params or {},ensure_ascii=False),model_template,provider,model,refer_image_count,status,error_message,json.dumps(outputs,ensure_ascii=False),json.dumps(inputs,ensure_ascii=False),str(task_id or ""),str(umo or ""),generation_time_ms,str(resolved_prompt or ""),json.dumps(options or {},ensure_ascii=False),1 if pending else 0))
            rid=int(cur.lastrowid)
            for kind,paths,thumbs in (("output",outputs,ots),("input",inputs,its)):
                for pos,path in enumerate(paths): conn.execute("INSERT INTO generation_assets(generation_id,kind,position,file_path,thumbnail_path) VALUES(?,?,?,?,?)",(rid,kind,pos,str(path),str(thumbs[pos] if pos<len(thumbs) else "")))
            return rid
    def _hydrate(self,conn,row):
        item=dict(row); item["params"]=self._loads(item.get("params"),{})
        item["options"]=self._loads(item.get("options"),{})
        if not isinstance(item["params"],dict): item["params"]={}
        if not isinstance(item["options"],dict): item["options"]={}
        item["resolved_prompt"]=item.get("resolved_prompt") or ""
        item["pending"]=bool(item.get("pending"))
        assets=conn.execute("SELECT kind,position,file_path,thumbnail_path FROM generation_assets WHERE generation_id=? ORDER BY kind,position",(item["id"],)).fetchall()
        if assets:
            for kind,pk,tk in (("output","image_paths","image_thumbnail_paths"),("input","source_image_paths","source_thumbnail_paths")):
                selected=[a for a in assets if a["kind"]==kind]; item[pk]=[a["file_path"] for a in selected]; item[tk]=[a["thumbnail_path"] or "" for a in selected]
        else:
            item["image_paths"]=self._loads(item.get("image_paths"),[]); item["source_image_paths"]=self._loads(item.get("source_image_paths"),[]); item["image_thumbnail_paths"]=[]; item["source_thumbnail_paths"]=[]
        return item
    def list(self,*,page=1,page_size=20,user_id="",model_template="",status="",start_time=0.0,end_time=0.0,keyword="",order="time_desc")->dict[str,Any]:
        page,page_size=max(1,int(page)),max(1,min(100,int(page_size))); cond=[]; vals=[]
        for col,val in (("user_id",user_id),("model_template",model_template)):
            if val: cond.append(f"{col} = ?"); vals.append(val)
        status=str(status or "").strip()
        if status=="pending":
            # 「排队中」有两种写法：status 直接是 pending，或 status 已是终态但 pending 标记
            # 还没清掉。两种都算排队中，否则列表里筛「排队中」会漏记录。
            cond.append("(status = 'pending' OR pending = 1)")
        elif status:
            cond.append("status = ?"); vals.append(status)
        if start_time>0: cond.append("timestamp >= ?"); vals.append(start_time)
        if end_time>0: cond.append("timestamp <= ?"); vals.append(end_time)
        keyword=str(keyword or "").strip()
        if keyword:
            hits=" OR ".join(f"{col} LIKE ? ESCAPE '\\'" for col in _KEYWORD_COLUMNS)
            cond.append(f"({hits})"); vals.extend([_like_pattern(keyword)]*len(_KEYWORD_COLUMNS))
        where=" WHERE "+" AND ".join(cond) if cond else ""
        ordering=_ORDERS.get(str(order or "").strip(),_ORDERS["time_desc"])
        with self._lock,self._connect() as conn:
            total=conn.execute(f"SELECT COUNT(*) FROM generations{where}",vals).fetchone()[0]
            rows=conn.execute(f"SELECT * FROM generations{where} ORDER BY {ordering} LIMIT ? OFFSET ?",vals+[page_size,(page-1)*page_size]).fetchall(); items=[self._hydrate(conn,r) for r in rows]
        return {"items":items,"total":total,"page":page,"page_size":page_size}
    def get(self,record_id:int)->Optional[dict]:
        with self._lock,self._connect() as conn:
            row=conn.execute("SELECT * FROM generations WHERE id=?",(int(record_id),)).fetchone(); return self._hydrate(conn,row) if row else None
    def update_images(self,record_id:int,*,image_paths=None,image_thumbnail_paths=None,status="success",error_message="",task_id=None,generation_time_ms=None)->bool:
        """回填某条记录的生成图（「找回 / 补发」成功后调用），并同步资源表。"""
        outputs=list(image_paths or []); thumbs=list(image_thumbnail_paths or [])
        with self._lock,self._connect() as conn:
            if conn.execute("SELECT 1 FROM generations WHERE id=?",(int(record_id),)).fetchone() is None:
                return False
            sets=["image_paths=?","status=?","error_message=?"]; vals=[json.dumps(outputs,ensure_ascii=False),status,error_message]
            if task_id: sets.append("task_id=?"); vals.append(str(task_id))
            if generation_time_ms is not None: sets.append("generation_time_ms=?"); vals.append(float(generation_time_ms))
            vals.append(int(record_id))
            conn.execute(f"UPDATE generations SET {', '.join(sets)} WHERE id=?",vals)
            conn.execute("DELETE FROM generation_assets WHERE generation_id=? AND kind='output'",(int(record_id),))
            for pos,path in enumerate(outputs):
                conn.execute("INSERT OR IGNORE INTO generation_assets(generation_id,kind,position,file_path,thumbnail_path) VALUES(?,?,?,?,?)",(int(record_id),"output",pos,str(path),str(thumbs[pos] if pos<len(thumbs) else "")))
            return True
    def update_extra(self,record_id:int,*,options=None,error_message=None,status=None)->bool:
        """更新一条记录的补充信息（选项快照 / 失败原因 / 状态），不碰图片。"""
        sets=[]; vals=[]
        if options is not None: sets.append("options=?"); vals.append(json.dumps(options or {},ensure_ascii=False))
        if error_message is not None: sets.append("error_message=?"); vals.append(str(error_message))
        if status is not None: sets.append("status=?"); vals.append(str(status))
        if not sets: return False
        vals.append(int(record_id))
        with self._lock,self._connect() as conn:
            cur=conn.execute(f"UPDATE generations SET {', '.join(sets)} WHERE id=?",vals)
            return bool(cur.rowcount)
    def finish_pending(self,record_id:int,*,image_paths=None,image_thumbnail_paths=None,status="success",error_message="",task_id=None,generation_time_ms=None)->bool:
        """排队中的记录出结果：回填图片，并解除「排队中」标记。"""
        with self._lock,self._connect() as conn:
            if conn.execute("SELECT 1 FROM generations WHERE id=?",(int(record_id),)).fetchone() is None:
                return False
        ok=self.update_images(int(record_id),image_paths=image_paths,image_thumbnail_paths=image_thumbnail_paths,status=status,error_message=error_message,task_id=task_id,generation_time_ms=generation_time_ms)
        if ok:
            with self._lock,self._connect() as conn:
                conn.execute("UPDATE generations SET pending=0 WHERE id=?",(int(record_id),))
        return ok
    def fail_pending(self,record_id:int,error_message:str,*,generation_time_ms=None)->bool:
        """排队中的记录最终失败：标记失败并解除「排队中」。"""
        with self._lock,self._connect() as conn:
            cur=conn.execute("UPDATE generations SET pending=0,status='failed',error_message=? WHERE id=?",(str(error_message or ""),int(record_id)))
            if generation_time_ms is not None:
                conn.execute("UPDATE generations SET generation_time_ms=? WHERE id=?",(float(generation_time_ms),int(record_id)))
            return bool(cur.rowcount)
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
        """分批删除。SQLite 默认最多 999 个绑定变量，整表一次 IN(...) 在记录多时会直接失败。"""
        ids=[int(i) for i in ids]
        if not ids:return 0
        deleted=0; paths=[]
        for start in range(0,len(ids),400):
            chunk=ids[start:start+400]
            marks=",".join("?" for _ in chunk)
            with self._lock,self._connect() as conn:
                assets=conn.execute(f"SELECT file_path,thumbnail_path FROM generation_assets WHERE generation_id IN ({marks})",chunk).fetchall()
                legacy=conn.execute(f"SELECT image_paths,source_image_paths FROM generations WHERE id IN ({marks})",chunk).fetchall()
                conn.execute(f"DELETE FROM generation_assets WHERE generation_id IN ({marks})",chunk)
                cur=conn.execute(f"DELETE FROM generations WHERE id IN ({marks})",chunk)
                deleted+=int(cur.rowcount or 0)
            paths+=[v for r in assets for v in (r["file_path"],r["thumbnail_path"])]
            for r in legacy: paths+=self._loads(r["image_paths"],[])+self._loads(r["source_image_paths"],[])
        for path in set(paths): self._safe_delete(path)
        return deleted
    def delete(self,record_id): return self._delete_many([int(record_id)])>0
    def clear(self):
        with self._lock,self._connect() as conn: ids=[r[0] for r in conn.execute("SELECT id FROM generations").fetchall()]
        return self._delete_many(ids)
    def enforce_limit(self,limit):
        limit=max(0,int(limit))
        deleted=0
        while True:
            with self._lock,self._connect() as conn:
                ids=[r[0] for r in conn.execute("SELECT id FROM generations ORDER BY timestamp DESC,id DESC LIMIT 500 OFFSET ?",(limit,)).fetchall()]
            if not ids: break
            deleted+=self._delete_many(ids)
        return deleted
    def stats(self):
        with self._lock,self._connect() as conn:
            total=conn.execute("SELECT COUNT(*) FROM generations").fetchone()[0]; success=conn.execute("SELECT COUNT(*) FROM generations WHERE status='success'").fetchone()[0]; failed=conn.execute("SELECT COUNT(*) FROM generations WHERE status='failed'").fetchone()[0]; pending=conn.execute("SELECT COUNT(*) FROM generations WHERE pending=1").fetchone()[0]; models=conn.execute("SELECT model_template,COUNT(*) cnt FROM generations GROUP BY model_template ORDER BY cnt DESC").fetchall()
        return {"total":total,"success":success,"failed":failed,"pending":pending,"by_model":[{"model":r["model_template"],"count":r["cnt"]} for r in models]}
