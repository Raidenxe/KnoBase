"""对话历史存储(SQLite, 线程安全), 支持会话的增删查与消息窗口读取。"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConversationStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations(
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    tenant_id TEXT DEFAULT '',
                    owner TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    citations TEXT DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                    ON messages(conversation_id, id);
                """
            )
            # 兼容旧库: 缺 tenant_id / owner 列时补充
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(conversations)")}
            if "tenant_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE conversations ADD COLUMN tenant_id TEXT DEFAULT ''"
                )
            if "owner" not in cols:
                self._conn.execute(
                    "ALTER TABLE conversations ADD COLUMN owner TEXT DEFAULT ''"
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    def _filter_clause(self, tenant_id: str, owner: str = "") -> tuple:
        conditions: List[str] = []
        args: List[Any] = []
        if tenant_id:
            conditions.append("tenant_id=?")
            args.append(tenant_id)
        if owner:
            conditions.append("owner=?")
            args.append(owner)
        if not conditions:
            return "", ()
        return " AND " + " AND ".join(conditions), tuple(args)

    def create_conversation(self, title: str, tenant_id: str = "", owner: str = "") -> Dict[str, Any]:
        conv_id = uuid.uuid4().hex[:16]
        ts = _now()
        with _LOCK:
            self._conn.execute(
                "INSERT INTO conversations(id,title,tenant_id,owner,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?)",
                (conv_id, title[:60], tenant_id, owner, ts, ts),
            )
            self._conn.commit()
        return {"id": conv_id, "title": title[:60], "created_at": ts, "updated_at": ts,
                "owner": owner}

    def get_conversation(self, conv_id: str, tenant_id: str = "", owner: str = "") -> Optional[Dict[str, Any]]:
        extra, args = self._filter_clause(tenant_id, owner)
        with _LOCK:
            row = self._conn.execute(
                f"SELECT * FROM conversations WHERE id=?{extra}", (conv_id, *args)
            ).fetchone()
        return dict(row) if row else None

    def list_conversations(self, limit: int = 50, tenant_id: str = "", owner: str = "") -> List[Dict[str, Any]]:
        extra, args = self._filter_clause(tenant_id, owner)
        with _LOCK:
            rows = self._conn.execute(
                f"""SELECT c.*, COUNT(m.id) AS message_count
                   FROM conversations c LEFT JOIN messages m ON m.conversation_id=c.id
                   WHERE 1=1{extra}
                   GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?""",
                (*args, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def rename_conversation(self, conv_id: str, title: str, tenant_id: str = "", owner: str = "") -> bool:
        extra, args = self._filter_clause(tenant_id, owner)
        title = (title or "").strip()[:60]
        if not title:
            return False
        with _LOCK:
            cur = self._conn.execute(
                f"UPDATE conversations SET title=?, updated_at=? WHERE id=?{extra}",
                (title, _now(), conv_id, *args),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_conversation(self, conv_id: str, tenant_id: str = "", owner: str = "") -> bool:
        extra, args = self._filter_clause(tenant_id, owner)
        with _LOCK:
            cur = self._conn.execute(
                f"DELETE FROM conversations WHERE id=?{extra}", (conv_id, *args)
            )
            self._conn.execute(
                "DELETE FROM messages WHERE conversation_id=?", (conv_id,)
            )
            self._conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    def append_message(
        self,
        conv_id: str,
        role: str,
        content: str,
        citations: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        ts = _now()
        with _LOCK:
            cur = self._conn.execute(
                "INSERT INTO messages(conversation_id,role,content,citations,created_at)"
                " VALUES(?,?,?,?,?)",
                (conv_id, role, content, json.dumps(citations or [], ensure_ascii=False), ts),
            )
            self._conn.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (ts, conv_id)
            )
            self._conn.commit()
        return {"id": cur.lastrowid, "role": role, "content": content,
                "citations": citations or [], "created_at": ts}

    def get_messages(self, conv_id: str) -> List[Dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id ASC",
                (conv_id,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "citations": json.loads(r["citations"] or "[]"),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def history_window(self, conv_id: str, window: int = 8) -> List[Dict[str, str]]:
        """取最近 window 条消息(升序), 供多轮上下文使用"""
        msgs = self.get_messages(conv_id)
        return [{"role": m["role"], "content": m["content"]} for m in msgs[-window:]]


_store: Optional[ConversationStore] = None


def get_conversation_store() -> ConversationStore:
    global _store
    if _store is None:
        from app.config import get_settings

        _store = ConversationStore(get_settings().history_db_path)
    return _store
