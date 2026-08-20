"""用户反馈存储(SQLite, 线程安全): 点赞/点踩 + 可选文字说明 + 统计。"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOCK = threading.Lock()


class FeedbackStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS feedback(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    tenant_id TEXT DEFAULT '',
                    rating TEXT NOT NULL,          -- 'up' | 'down'
                    comment TEXT DEFAULT '',
                    created_by TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_conv
                    ON feedback(conversation_id, message_id);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    def submit(
        self,
        conversation_id: str,
        message_id: int,
        rating: str,
        comment: str = "",
        tenant_id: str = "",
        created_by: str = "",
    ) -> Dict[str, Any]:
        """提交或更新反馈(同一消息可改, 更新时保留最新)。"""
        from datetime import datetime, timezone

        if rating not in ("up", "down"):
            raise ValueError("rating 必须为 up 或 down")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _LOCK:
            row = self._conn.execute(
                "SELECT id FROM feedback WHERE conversation_id=? AND message_id=?",
                (conversation_id, message_id),
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE feedback SET rating=?, comment=?, created_by=?, created_at=?"
                    " WHERE id=?",
                    (rating, comment, created_by, ts, row["id"]),
                )
                fb_id = row["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO feedback(conversation_id,message_id,tenant_id,"
                    " rating,comment,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (conversation_id, message_id, tenant_id, rating, comment,
                     created_by, ts),
                )
                fb_id = cur.lastrowid
            self._conn.commit()
        return {"id": fb_id, "conversation_id": conversation_id,
                "message_id": message_id, "rating": rating, "comment": comment,
                "created_at": ts}

    def get(self, conversation_id: str, message_id: int) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM feedback WHERE conversation_id=? AND message_id=?",
                (conversation_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def stats(self, tenant_id: str = "") -> Dict[str, Any]:
        extra, args = (" WHERE tenant_id=?", (tenant_id,)) if tenant_id else ("", ())
        with _LOCK:
            rows = self._conn.execute(
                f"SELECT rating, COUNT(*) AS c FROM feedback{extra} GROUP BY rating",
                args,
            ).fetchall()
        total = sum(r["c"] for r in rows)
        up = next((r["c"] for r in rows if r["rating"] == "up"), 0)
        down = next((r["c"] for r in rows if r["rating"] == "down"), 0)
        return {
            "total": total, "up": up, "down": down,
            "up_ratio": round(up / total, 4) if total else 0.0,
        }

    def recent(self, limit: int = 50, tenant_id: str = "") -> List[Dict[str, Any]]:
        extra, args = (" WHERE tenant_id=?", (tenant_id,)) if tenant_id else ("", ())
        with _LOCK:
            rows = self._conn.execute(
                f"SELECT * FROM feedback{extra} ORDER BY id DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [dict(r) for r in rows]


_store: Optional[FeedbackStore] = None


def get_feedback_store() -> FeedbackStore:
    global _store
    if _store is None:
        from app.config import get_settings

        _store = FeedbackStore(get_settings().history_db_path)
    return _store