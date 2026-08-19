"""知识库管理面板-文档记录存储(SQLite kb_docs.db)。

仅"经管理面板上传/覆盖/重试"的文档在此登记一条记录, 用于支撑 MVP CRUD 的
状态机(processing → done / failed) + 失败原因 + 展示名 + 软件版本号 + 源文件路径。

与 Milvus 的关系(设计约束):
    - Milvus 是已向量化文档的权威事实源(list 以此为准);
    - 本表是管理视图的补充事实: 记录成功/处理中/失败三类, 其中"成功"与 Milvus 对齐,
      "处理中/失败"仅存在于本表(未真正入向量库), 供前端展示状态与失败原因;
    - 历史/目录扫描导入的文档可能无记录, list 时按默认(done)兜底, 不强制回填。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from app.config import get_settings

_LOCK = threading.RLock()

STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUSES = {STATUS_PROCESSING, STATUS_DONE, STATUS_FAILED}

# 管理面板允许上传的格式(取自底层 loader 交集, 见 app.knowledge.loader.SUPPORTED_EXTS)
# 仅透出用户要求的四类: PDF / Word / TXT / Markdown
SUPPORTED_UPLOAD_EXTS = {".pdf", ".docx", ".txt", ".md", ".markdown"}


def format_of(path: str) -> str:
    """由文件名推断展示格式(md/word/pdf/txt)。"""
    ext = _ext(path)
    if ext in {".md", ".markdown"}:
        return "md"
    if ext == ".docx":
        return "word"
    if ext == ".pdf":
        return "pdf"
    if ext == ".txt":
        return "txt"
    return "other"


def _ext(path: str) -> str:
    import os

    return os.path.splitext(path)[1].lower()


class DocRecordStore:
    def __init__(self, db_path: str) -> None:
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kb_doc_records(
                    doc_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    version TEXT DEFAULT '',
                    source_path TEXT DEFAULT '',
                    format TEXT DEFAULT '',
                    size INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'processing',
                    error TEXT DEFAULT '',
                    created_at REAL,
                    updated_at REAL,
                    PRIMARY KEY(doc_id, tenant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_kbdoc_rec_tenant ON kb_doc_records(tenant_id, created_at);
                """
            )
            self._conn.commit()

    def register(
        self, doc_id: str, tenant_id: str, display_name: str,
        format: str = "", size: int = 0, source_path: str = "",
    ) -> Dict[str, Any]:
        """登记一条处理中记录(覆盖则重置状态)。"""
        now = time.time()
        with _LOCK:
            self._conn.execute(
                "INSERT INTO kb_doc_records(doc_id, tenant_id, display_name, version, "
                "source_path, format, size, status, error, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(doc_id, tenant_id) DO UPDATE SET "
                "display_name=excluded.display_name, source_path=excluded.source_path, "
                "format=excluded.format, size=excluded.size, status='processing', "
                "error='', updated_at=excluded.updated_at",
                (doc_id, tenant_id, display_name, "", source_path, format, int(size),
                 STATUS_PROCESSING, "", now, now),
            )
            self._conn.commit()
        return {"doc_id": doc_id, "status": STATUS_PROCESSING}

    def set_status(self, doc_id: str, tenant_id: str, status: str, error: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"非法状态: {status}")
        with _LOCK:
            self._conn.execute(
                "UPDATE kb_doc_records SET status=?, error=?, updated_at=? "
                "WHERE doc_id=? AND tenant_id=?",
                (status, error, time.time(), doc_id, tenant_id),
            )
            self._conn.commit()

    def get(self, doc_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT doc_id, display_name, version, source_path, format, size, "
                "status, error, created_at, updated_at FROM kb_doc_records "
                "WHERE doc_id=? AND tenant_id=?",
                (doc_id, tenant_id),
            ).fetchone()
        if not row:
            return None
        return {
            "doc_id": row[0], "display_name": row[1] or "", "version": row[2] or "",
            "source_path": row[3] or "", "format": row[4] or "",
            "size": row[5] or 0, "status": row[6], "error": row[7] or "",
            "created_at": row[8], "updated_at": row[9],
        }

    def get_many(self, doc_ids: List[str], tenant_id: str) -> Dict[str, Dict[str, Any]]:
        if not doc_ids:
            return {}
        placeholders = ",".join("?" for _ in doc_ids)
        with _LOCK:
            rows = self._conn.execute(
                f"SELECT doc_id, display_name, version, source_path, format, size, "
                f"status, error, created_at, updated_at FROM kb_doc_records "
                f"WHERE tenant_id=? AND doc_id IN ({placeholders})",
                (tenant_id, *doc_ids),
            ).fetchall()
        out: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            out[row[0]] = {
                "doc_id": row[0], "display_name": row[1] or "", "version": row[2] or "",
                "source_path": row[3] or "", "format": row[4] or "",
                "size": row[5] or 0, "status": row[6], "error": row[7] or "",
                "created_at": row[8], "updated_at": row[9],
            }
        return out

    def list_all(self, tenant_id: str, limit: int = 10000) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT doc_id, display_name, version, source_path, format, size, "
                "status, error, created_at, updated_at FROM kb_doc_records "
                "WHERE tenant_id=? ORDER BY COALESCE(created_at,0) DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [
            {
                "doc_id": r[0], "display_name": r[1] or "", "version": r[2] or "",
                "source_path": r[3] or "", "format": r[4] or "",
                "size": r[5] or 0, "status": r[6], "error": r[7] or "",
                "created_at": r[8], "updated_at": r[9],
            }
            for r in rows
        ]

    def update_profile(
        self, doc_id: str, tenant_id: str,
        display_name: Optional[str] = None, version: Optional[str] = None,
    ) -> Dict[str, Any]:
        cur = self.get(doc_id, tenant_id) or {}
        new_name = display_name if display_name is not None else cur.get("display_name", "")
        new_ver = version if version is not None else cur.get("version", "")
        with _LOCK:
            self._conn.execute(
                "INSERT INTO kb_doc_records(doc_id, tenant_id, display_name, version, "
                "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(doc_id, tenant_id) DO UPDATE SET "
                "display_name=excluded.display_name, version=excluded.version, "
                "updated_at=excluded.updated_at",
                (doc_id, tenant_id, new_name, new_ver, STATUS_DONE, time.time(), time.time()),
            )
            self._conn.commit()
        return {"doc_id": doc_id, "display_name": new_name, "version": new_ver,
                "status": cur.get("status", STATUS_DONE)}

    def delete(self, doc_id: str, tenant_id: str) -> None:
        with _LOCK:
            self._conn.execute(
                "DELETE FROM kb_doc_records WHERE doc_id=? AND tenant_id=?",
                (doc_id, tenant_id),
            )
            self._conn.commit()


_store: Optional[DocRecordStore] = None
_store_lock = threading.Lock()


def get_doc_record_store() -> DocRecordStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DocRecordStore(get_settings().doc_records_db_path)
    return _store