"""会话分享(SQLite): 为会话生成只读分享链接, 支持过期与删除。

分享链接无鉴权, 仅存会话内容快照(避免原会话被删后链接失效),
由公开即读的只读页 /share-html/{token} 或 JSON /api/v1/shares/{token} 渲染。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ShareStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shares(
                    token TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    tenant_id TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    created_by TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at INTEGER DEFAULT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shares_conv ON shares(conversation_id);
                """
            )
            self._conn.commit()

    def create(
        self,
        conversation_id: str,
        tenant_id: str,
        title: str,
        messages: List[Dict[str, Any]],
        created_by: str = "",
        ttl_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        import json

        token = uuid.uuid4().hex[:16]
        expires_at = int(time.time()) + ttl_seconds if ttl_seconds else None
        snap = json.dumps(messages, ensure_ascii=False)
        with _LOCK:
            self._conn.execute(
                "INSERT INTO shares(token,conversation_id,tenant_id,title,snapshot,"
                " created_by,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
                (token, conversation_id, tenant_id, title[:60], snap,
                 created_by, _now(), expires_at),
            )
            self._conn.commit()
        return {"token": token, "conversation_id": conversation_id,
                "title": title[:60], "expires_at": expires_at,
                "url": f"/s/{token}"}

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        import json

        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM shares WHERE token=?", (token,)
            ).fetchone()
        if not row:
            return None
        if row["expires_at"] and int(time.time()) > row["expires_at"]:
            self.delete(token)
            return None
        return {
            "token": row["token"],
            "conversation_id": row["conversation_id"],
            "tenant_id": row["tenant_id"],
            "title": row["title"],
            "messages": json.loads(row["snapshot"] or "[]"),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def list_by_conversation(self, conversation_id: str) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT token, title, created_at FROM shares"
                " WHERE conversation_id=? ORDER BY created_at DESC",
                (conversation_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, token: str) -> bool:
        with _LOCK:
            cur = self._conn.execute("DELETE FROM shares WHERE token=?", (token,))
            self._conn.commit()
        return cur.rowcount > 0

    def delete_by_conversation(self, conversation_id: str) -> None:
        with _LOCK:
            self._conn.execute(
                "DELETE FROM shares WHERE conversation_id=?", (conversation_id,)
            )
            self._conn.commit()


_share_store: Optional[ShareStore] = None


def get_share_store() -> ShareStore:
    global _share_store
    if _share_store is None:
        from app.config import get_settings

        _share_store = ShareStore(get_settings().shares_db_path)
    return _share_store