"""通知中心 SQLite 存储(notify.db)。

延续本项目"轻量 SQLite + 零迁移"的元数据存储惯例:
- 通知按接收人 user_id 分发(工单动态→提交人; 公告/知识库更新→全体或指定组)
- 租户隔离: 每条通知携带 tenant_id, 查询必须携带当前租户
- 保留策略: 超过 notify_retention_days 的已读/过期通知自动清理(惰性)
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.services.auth_store import get_auth_store

_LOCK = threading.RLock()

# 通知类型
T_TICKET_STATUS = "ticket_status"   # 工单状态变更
T_TICKET_REPLY = "ticket_reply"     # 管理员回复工单
T_KB_UPDATE = "kb_update"           # 知识库更新公告
T_ANNOUNCE = "announce"             # 系统维护公告
T_PERMISSION = "permission"         # 权限变更提醒


class NotifyStore:
    def __init__(self, db_path: str) -> None:
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS notifications(
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT DEFAULT '',
                    link TEXT DEFAULT '',
                    is_read INTEGER DEFAULT 0,
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_notif_user
                    ON notifications(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_notif_tenant
                    ON notifications(tenant_id);
                """
            )
            self._conn.commit()

    # ---------------- 创建 ----------------
    def _insert(self, obj: Dict[str, Any]) -> str:
        nid = _new_id()
        now = time.time()
        with _LOCK:
            self._conn.execute(
                "INSERT INTO notifications"
                "(id, tenant_id, user_id, type, title, content, link, is_read, created_at) "
                "VALUES(?,?,?,?,?,?,?,0,?)",
                (nid, obj["tenant_id"], obj["user_id"], obj["type"],
                 obj["title"], obj.get("content", ""), obj.get("link", ""), now),
            )
            self._conn.commit()
        return nid

    def notify_user(self, tenant_id: str, user_id: str, ntype: str,
                    title: str, content: str = "", link: str = "") -> str:
        """向单个用户推送通知。"""
        return self._insert({"tenant_id": tenant_id, "user_id": user_id,
                             "type": ntype, "title": title, "content": content, "link": link})

    def notify_users(self, tenant_id: str, user_ids: List[str], ntype: str,
                     title: str, content: str = "", link: str = "") -> int:
        """向一批用户推送同一通知(如全体公告、知识库更新)。"""
        n = 0
        for uid in dict.fromkeys(user_ids or []):
            if not uid:
                continue
            self._insert({"tenant_id": tenant_id, "user_id": uid, "type": ntype,
                          "title": title, "content": content, "link": link})
            n += 1
        return n

    # ---------------- 查询 ----------------
    @staticmethod
    def _row(r) -> Dict[str, Any]:
        return {
            "id": r["id"], "type": r["type"], "title": r["title"],
            "content": r["content"] or "", "link": r["link"] or "",
            "is_read": bool(r["is_read"]), "created_at": r["created_at"],
        }

    def list_notifications(self, tenant_id: str, user_id: str,
                           limit: int = 20, offset: int = 0,
                           ntype: Optional[str] = None) -> List[Dict[str, Any]]:
        if not user_id:
            return []
        sql = "SELECT * FROM notifications WHERE tenant_id=? AND user_id=?"
        args: List[Any] = [tenant_id, user_id]
        if ntype:
            sql += " AND type=?"
            args.append(ntype)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [max(1, min(limit, 100)), max(0, offset)]
        with _LOCK:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def recent(self, tenant_id: str, user_id: str, n: int = 5) -> List[Dict[str, Any]]:
        """下拉面板最近 n 条(含已读/未读, 按时间倒序)。"""
        if not user_id:
            return []
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM notifications WHERE tenant_id=? AND user_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (tenant_id, user_id, max(1, n)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def unread_count(self, tenant_id: str, user_id: str) -> int:
        if not user_id:
            return 0
        with _LOCK:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE tenant_id=? AND user_id=? AND is_read=0",
                (tenant_id, user_id),
            ).fetchone()
        return int(row[0] or 0)

    def get(self, notify_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM notifications WHERE id=? AND user_id=?", (notify_id, user_id)
            ).fetchone()
        return self._row(row) if row else None

    def mark_read(self, notify_id: str, user_id: str) -> bool:
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
                (notify_id, user_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def mark_all_read(self, tenant_id: str, user_id: str) -> int:
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE notifications SET is_read=1 WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_id),
            )
            self._conn.commit()
        return cur.rowcount

    def delete(self, notify_id: str, user_id: str) -> bool:
        with _LOCK:
            cur = self._conn.execute(
                "DELETE FROM notifications WHERE id=? AND user_id=?", (notify_id, user_id)
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_many(self, ids: List[str], user_id: str) -> int:
        if not ids:
            return 0
        ph = ",".join("?" for _ in ids)
        with _LOCK:
            cur = self._conn.execute(
                f"DELETE FROM notifications WHERE id IN ({ph}) AND user_id=?",
                (*ids, user_id),
            )
            self._conn.commit()
        return cur.rowcount

    # ---------------- 广播公告(通知全体/某组) ----------------
    def make_broadcast(self, tenant_id: str, user_ids: List[str], ntype: str,
                       title: str, content: str = "", link: str = "") -> int:
        """广播: 写入 notifications 表(等同 notify_users)。"""
        return self.notify_users(tenant_id, user_ids, ntype, title, content, link)

    def list_tenants(self) -> List[str]:
        with _LOCK:
            rows = self._conn.execute("SELECT DISTINCT tenant_id FROM notifications").fetchall()
        return [r["tenant_id"] for r in rows]

    def delete_tenant(self, tenant_id: str) -> None:
        with _LOCK:
            self._conn.execute("DELETE FROM notifications WHERE tenant_id=?", (tenant_id,))
            self._conn.commit()

    # ---------------- 保留策略 ----------------
    def purge_expired(self, retention_days: int = 90) -> int:
        """清理超过 retention_days 的通知(已读优先保留更久, 简单起见统一按天数)。"""
        cutoff = time.time() - retention_days * 86400
        with _LOCK:
            cur = self._conn.execute(
                "DELETE FROM notifications WHERE created_at < ?", (cutoff,)
            )
            self._conn.commit()
        return cur.rowcount


# 广播/公告与私有通知合并展示: recent 查询用 UNION 引用 broadcasts 表, 这里统一在同一库定义
def _new_id() -> str:
    import uuid

    return "N" + uuid.uuid4().hex[:20]


_store: Optional[NotifyStore] = None
_store_lock = threading.Lock()


def get_notify_store() -> NotifyStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = NotifyStore(get_settings().notify_db_path)
    return _store


def tenant_user_ids(tenant_id: str, group_ids: Optional[List[str]] = None) -> List[str]:
    """取某租户下应收到广播通知的用户 id 列表。

    - 未指定用户组 -> 租户内所有启用用户
    - 指定用户组 -> 组内成员(并集, 去重)
    认证关闭(演示模式)时通知不落库, 返回空。
    """
    if not _auth_on():
        return []
    store = get_auth_store()
    if not group_ids:
        return [u["id"] for u in store.list_users(tenant_id) if u.get("is_active")]
    seen: set = set()
    out: List[str] = []
    for gid in group_ids:
        for m in store.group_members(gid):
            uid = m["id"]
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
    return out


def _auth_on() -> bool:
    return get_settings().auth_mode.lower() == "on"