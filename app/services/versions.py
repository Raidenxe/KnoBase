"""文档版本管理(SQLite): 记录每个文档的修订历史与全文快照。

- Milvus 只保留"当前激活版本"的向量; 历史版本全文以快照形式存于本库
- 支持: 版本列表 / 指定版本全文 / 回滚(用快照恢复为当前激活)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


class DocVersionStore:
    def __init__(self, db_path: str) -> None:
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS doc_versions(
                    doc_id TEXT,
                    doc_name TEXT,
                    version INTEGER,
                    fingerprint TEXT,
                    chunk_count INTEGER,
                    source_file TEXT,
                    tenant_id TEXT DEFAULT '',
                    created_by TEXT DEFAULT '',
                    created_at TEXT,
                    PRIMARY KEY(doc_id, version)
                );
                CREATE TABLE IF NOT EXISTS doc_version_chunks(
                    doc_id TEXT,
                    version INTEGER,
                    chunk_index INTEGER,
                    section_path TEXT,
                    page INTEGER,
                    text TEXT,
                    PRIMARY KEY(doc_id, version, chunk_index)
                );
                CREATE INDEX IF NOT EXISTS idx_doc_versions_doc
                    ON doc_versions(doc_id);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    def save_version(
        self,
        doc_id: str,
        doc_name: str,
        fingerprint: str,
        chunk_count: int,
        source_file: str,
        chunks: List[Dict[str, Any]],
        tenant_id: str = "",
        created_by: str = "",
    ) -> int:
        """保存一次修订, 返回版本号。指纹与上一版本一致时直接复用(不新增)。"""
        from datetime import datetime, timezone

        with _LOCK:
            row = self._conn.execute(
                "SELECT version, fingerprint, created_at FROM doc_versions"
                " WHERE doc_id=? ORDER BY version DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
            if row and row["fingerprint"] == fingerprint:
                return int(row["version"])
            new_version = int(row["version"]) + 1 if row else 1
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self._conn.execute(
                "INSERT INTO doc_versions(doc_id,doc_name,version,fingerprint,"
                " chunk_count,source_file,tenant_id,created_by,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (doc_id, doc_name, new_version, fingerprint, chunk_count,
                 source_file, tenant_id, created_by, ts),
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO doc_version_chunks"
                "(doc_id,version,chunk_index,section_path,page,text)"
                " VALUES(?,?,?,?,?,?)",
                [
                    (doc_id, new_version, c["chunk_index"], c.get("section_path") or "",
                     c.get("page") or -1, c["text"])
                    for c in chunks
                ],
            )
            self._conn.commit()
        return new_version

    # ------------------------------------------------------------------
    def latest(self, doc_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM doc_versions WHERE doc_id=? ORDER BY version DESC LIMIT 1",
                (doc_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_versions(self, doc_id: str) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM doc_versions WHERE doc_id=? ORDER BY version DESC",
                (doc_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, doc_id: str, version: int) -> Optional[Dict[str, Any]]:
        with _LOCK:
            meta = self._conn.execute(
                "SELECT * FROM doc_versions WHERE doc_id=? AND version=?",
                (doc_id, version),
            ).fetchone()
            if not meta:
                return None
            chunks = self._conn.execute(
                "SELECT chunk_index, section_path, page, text FROM doc_version_chunks"
                " WHERE doc_id=? AND version=? ORDER BY chunk_index ASC",
                (doc_id, version),
            ).fetchall()
        return {**dict(meta), "chunks": [dict(c) for c in chunks]}

    def delete_doc(self, doc_id: str) -> None:
        with _LOCK:
            self._conn.execute("DELETE FROM doc_versions WHERE doc_id=?", (doc_id,))
            self._conn.execute(
                "DELETE FROM doc_version_chunks WHERE doc_id=?", (doc_id,)
            )
            self._conn.commit()


_store: Optional[DocVersionStore] = None


def get_version_store() -> DocVersionStore:
    global _store
    if _store is None:
        from app.config import get_settings

        _store = DocVersionStore(get_settings().doc_versions_db_path)
    return _store