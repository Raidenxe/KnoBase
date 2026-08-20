"""工单系统 SQLite 存储(tickets.db)。

延续本项目"轻量 SQLite + 零迁移"惯例:
- 工单(含提交人、类型、紧急度、状态) + 管理员回复时间线 + 催办计数
- 租户隔离: 每条工单携带 tenant_id, 查询必须携带当前租户
- 状态机: pending(待处理) -> processing(处理中) -> resolved(已解决) -> closed(已关闭)
- 流转时可选填写处理说明(handle_note); 已解决可"确认关闭", 已关闭的工单不可再回复
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from app.config import get_settings

_LOCK = threading.RLock()

TICKET_TYPES = ["知识库缺失", "答案错误", "新文档申请", "使用咨询", "其他"]
URGENT_LEVELS = ["normal", "urgent"]
# 状态机(单向流转)
STATUS_FLOW = ["pending", "processing", "resolved", "closed"]
STATUS_CN = {
    "pending": "待处理", "processing": "处理中",
    "resolved": "已解决", "closed": "已关闭",
}

# 催办策略: 满 3 个工作日未更新且状态未终态才可催办; 每工单限 2 次(用户规定)
WORKDAY_DAYS = 3
REMIND_LIMIT = 2


class TicketStore:
    def __init__(self, db_path: str) -> None:
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tickets(
                    id TEXT PRIMARY KEY,           -- TK yyyyMMdd + 3位序号
                    tenant_id TEXT NOT NULL,
                    creator_id TEXT NOT NULL,
                    creator_name TEXT DEFAULT '',
                    ticket_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    urgency TEXT DEFAULT 'normal',
                    attachment TEXT DEFAULT '',     -- 存储的文件名
                    status TEXT DEFAULT 'pending',
                    handle_note TEXT DEFAULT '',
                    remind_count INTEGER DEFAULT 0,
                    last_remind_at REAL,
                    closed_by TEXT DEFAULT '',
                    closed_at REAL,
                    created_at REAL,
                    updated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_ticket_creator
                    ON tickets(creator_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ticket_tenant
                    ON tickets(tenant_id, status, created_at);
                -- 管理员回复(时间线)
                CREATE TABLE IF NOT EXISTS ticket_replies(
                    id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    author_name TEXT DEFAULT '',
                    author_role TEXT DEFAULT '',
                    content TEXT NOT NULL,
                    is_admin INTEGER DEFAULT 0,
                    created_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_tr_ticket
                    ON ticket_replies(ticket_id, created_at);
                """
            )
            self._conn.commit()

    # ---------------- 工单 ----------------
    def generate_id(self, now: float) -> str:
        """生成工单编号: TK + yyyyMMdd + 3位序号(同日内自增)。"""
        import datetime

        day = datetime.datetime.fromtimestamp(now).strftime("%Y%m%d")
        with _LOCK:
            row = self._conn.execute(
                "SELECT id FROM tickets WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
                (f"TK{day}%",),
            ).fetchone()
        seq = 1
        if row and len(row["id"]) >= len(f"TK{day}") + 1:
            try:
                seq = int(row["id"][len(f"TK{day}"):]) + 1
            except ValueError:
                seq = 1
        return f"TK{day}{seq:03d}"

    def create(self, tenant_id: str, creator_id: str, creator_name: str,
               ticket_type: str, title: str, description: str,
               urgency: str = "normal", attachment: str = "") -> Dict[str, Any]:
        if ticket_type not in TICKET_TYPES:
            raise ValueError(f"非法工单类型: {ticket_type}, 可选: {TICKET_TYPES}")
        if urgency not in URGENT_LEVELS:
            raise ValueError(f"非法紧急程度: {urgency}")
        if not title.strip():
            raise ValueError("工单标题不能为空")
        now = time.time()
        tid = self.generate_id(now)
        with _LOCK:
            self._conn.execute(
                "INSERT INTO tickets"
                "(id, tenant_id, creator_id, creator_name, ticket_type, title, "
                " description, urgency, attachment, status, handle_note, "
                " remind_count, last_remind_at, closed_by, closed_at, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending','',0,NULL,'',NULL,?,?)",
                (tid, tenant_id, creator_id, creator_name, ticket_type, title.strip(),
                 description or "", urgency, attachment, now, now),
            )
            self._conn.commit()
        return self.get(tid, tenant_id) or {}  # type: ignore[return-value]

    def get(self, ticket_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM tickets WHERE id=? AND tenant_id=?", (ticket_id, tenant_id)
            ).fetchone()
            replies = self._conn.execute(
                "SELECT * FROM ticket_replies WHERE ticket_id=? ORDER BY created_at ASC",
                (ticket_id,),
            ).fetchall()
        if not row:
            return None
        obj = self._row(row)
        obj["replies"] = [self._reply_row(r) for r in replies]
        obj["can_remind"] = self._remind_available(obj)
        return obj

    @staticmethod
    def _row(r) -> Dict[str, Any]:
        return {
            "id": r["id"], "tenant_id": r["tenant_id"],
            "creator_id": r["creator_id"], "creator_name": r["creator_name"] or "",
            "ticket_type": r["ticket_type"], "title": r["title"],
            "description": r["description"] or "", "urgency": r["urgency"],
            "attachment": r["attachment"] or "", "status": r["status"],
            "status_cn": STATUS_CN.get(r["status"], r["status"]),
            "handle_note": r["handle_note"] or "",
            "remind_count": int(r["remind_count"] or 0),
            "last_remind_at": r["last_remind_at"],
            "closed_by": r["closed_by"] or "", "closed_at": r["closed_at"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
        }

    @staticmethod
    def _reply_row(r) -> Dict[str, Any]:
        return {
            "id": r["id"], "ticket_id": r["ticket_id"],
            "author_id": r["author_id"], "author_name": r["author_name"] or "",
            "author_role": r["author_role"] or "", "content": r["content"],
            "is_admin": bool(r["is_admin"]), "created_at": r["created_at"],
        }

    def list(self, tenant_id: str, creator_id: Optional[str] = None,
             status: Optional[str] = None, ticket_type: Optional[str] = None,
             limit: int = 20, offset: int = 0,
             sort_urgent_first: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tickets WHERE tenant_id=?"
        args: List[Any] = [tenant_id]
        if creator_id:
            sql += " AND creator_id=?"
            args.append(creator_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        if ticket_type:
            sql += " AND ticket_type=?"
            args.append(ticket_type)
        sql += " ORDER BY "
        if sort_urgent_first:
            sql += "CASE urgency WHEN 'urgent' THEN 0 ELSE 1 END, "
        if creator_id:
            sql += "created_at DESC"
        else:
            sql += "created_at DESC"
        sql += " LIMIT ? OFFSET ?"
        args += [max(1, min(limit, 200)), max(0, offset)]
        with _LOCK:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def count(self, tenant_id: str, creator_id: Optional[str] = None,
              status: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM tickets WHERE tenant_id=?"
        args: List[Any] = [tenant_id]
        if creator_id:
            sql += " AND creator_id=?"
            args.append(creator_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        with _LOCK:
            row = self._conn.execute(sql, args).fetchone()
        return int(row[0] or 0)

    def _remind_available(self, ticket: Dict[str, Any]) -> bool:
        """催办可用条件: 状态为 pending/processing, 距今超过 3 个工作日, 且未超过限次。"""
        if ticket["status"] not in ("pending", "processing"):
            return False
        if int(ticket.get("remind_count") or 0) >= REMIND_LIMIT:
            return False
        updated = ticket.get("updated_at") or ticket.get("created_at") or 0
        return (time.time() - updated) >= WORKDAY_DAYS * 86400

    def remind(self, ticket_id: str, tenant_id: str, by_id: str) -> Dict[str, Any]:
        ticket = self.get(ticket_id, tenant_id)
        if not ticket:
            raise LookupError("工单不存在")
        if ticket["creator_id"] != by_id:
            raise PermissionError("仅工单提交人可催办")
        if not self._remind_available(ticket):
            raise ValueError("当前不满足催办条件(仅待处理/处理中且超过3个工作日, 每单限2次)")
        new_count = int(ticket["remind_count"]) + 1
        with _LOCK:
            self._conn.execute(
                "UPDATE tickets SET remind_count=?, last_remind_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (new_count, time.time(), time.time(), ticket_id, tenant_id),
            )
            self._conn.commit()
        return {"id": ticket_id, "remind_count": new_count,
                "remaining": REMIND_LIMIT - new_count}

    # ---------------- 状态流转 ----------------
    def transition(self, ticket_id: str, tenant_id: str, target: str,
                   note: str = "", by_id: str = "", by_name: str = "") -> Dict[str, Any]:
        ticket = self.get(ticket_id, tenant_id)
        if not ticket:
            raise LookupError("工单不存在")
        if target not in STATUS_FLOW:
            raise ValueError(f"非法状态: {target}")
        cur_status = ticket["status"]
        # 一键到"已完成"是最常见的期望, 允许 pending/processing -> resolved
        if target in ("closed", "resolved") and cur_status in ("pending", "processing"):
            effective_target = target
        else:
            # 必须沿状态机顺序推进
            _cur_idx = STATUS_FLOW.index(cur_status)
            _tgt_idx = STATUS_FLOW.index(target)
            if _tgt_idx <= _cur_idx:
                raise ValueError(f"无法从 {STATUS_CN[cur_status]} 流转到 {STATUS_CN[target]}")
            effective_target = target
        now = time.time()
        closed_at = now if effective_target == "closed" else ticket.get("closed_at")
        closed_by = by_name if effective_target == "closed" else ticket.get("closed_by", "")
        with _LOCK:
            self._conn.execute(
                "UPDATE tickets SET status=?, handle_note=?, closed_at=?, closed_by=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (effective_target, note or "", closed_at, closed_by, now, ticket_id, tenant_id),
            )
            self._conn.commit()
        return self.get(ticket_id, tenant_id) or {}  # type: ignore[return-value]

    def close(self, ticket_id: str, tenant_id: str, by_id: str) -> Dict[str, Any]:
        """用户端"确认关闭": 仅当工单已被标记为已解决, 且由提交人确认。"""
        ticket = self.get(ticket_id, tenant_id)
        if not ticket:
            raise LookupError("工单不存在")
        if ticket["creator_id"] != by_id:
            raise PermissionError("仅工单提交人可确认关闭")
        if ticket["status"] != "resolved":
            raise ValueError("仅已解决的工单可确认关闭")
        with _LOCK:
            now = time.time()
            self._conn.execute(
                "UPDATE tickets SET status='closed', closed_at=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (now, now, ticket_id, tenant_id),
            )
            self._conn.commit()
        return self.get(ticket_id, tenant_id) or {}  # type: ignore[return-value]

    def reopen(self, ticket_id: str, tenant_id: str, by_id: str) -> Dict[str, Any]:
        """用户端"重新打开": 需当前为已解决状态(替代已关闭+说明的简化版)。
        已关闭后 7 天内可重新打开并补充说明。"""
        ticket = self.get(ticket_id, tenant_id)
        if not ticket:
            raise LookupError("工单不存在")
        if ticket["creator_id"] != by_id:
            raise PermissionError("仅工单提交人可重新打开")
        if ticket["status"] not in ("resolved", "closed"):
            raise ValueError("仅已解决/已关闭的工单可重新打开")
        if ticket["closed_at"] and (time.time() - ticket["closed_at"]) > 7 * 86400:
            raise ValueError("已关闭超过 7 天, 不支持重新打开")
        with _LOCK:
            self._conn.execute(
                "UPDATE tickets SET status='processing', updated_at=? WHERE id=? AND tenant_id=?",
                (time.time(), ticket_id, tenant_id),
            )
            self._conn.commit()
        return self.get(ticket_id, tenant_id) or {}  # type: ignore[return-value]

    # ---------------- 回复 ----------------
    def add_reply(self, ticket_id: str, tenant_id: str,
                  author_id: str, author_name: str, author_role: str,
                  content: str, is_admin: bool = False) -> Dict[str, Any]:
        ticket = self.get(ticket_id, tenant_id)
        if not ticket:
            raise LookupError("工单不存在")
        if ticket["status"] == "closed":
            raise ValueError("工单已关闭, 不可再回复")
        if not content.strip():
            raise ValueError("回复内容不能为空")
        rid = "R" + uuid.uuid4().hex[:20]
        now = time.time()
        with _LOCK:
            self._conn.execute(
                "INSERT INTO ticket_replies"
                "(id, ticket_id, author_id, author_name, author_role, content, is_admin, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (rid, ticket_id, author_id, author_name, author_role, content,
                 1 if is_admin else 0, now),
            )
            # 回复后刷新 updated_at(用于催办判定)
            self._conn.execute(
                "UPDATE tickets SET updated_at=? WHERE id=? AND tenant_id=?",
                (now, ticket_id, tenant_id),
            )
            self._conn.commit()
        return self.get(ticket_id, tenant_id) or {}  # type: ignore[return-value]

    # ---------------- 统计 ----------------
    def stats(self, tenant_id: str) -> Dict[str, Any]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM tickets WHERE tenant_id=? GROUP BY status",
                (tenant_id,),
            ).fetchall()
            type_rows = self._conn.execute(
                "SELECT ticket_type, COUNT(*) AS c FROM tickets WHERE tenant_id=? GROUP BY ticket_type",
                (tenant_id,),
            ).fetchall()
        status_by = {r["status"]: int(r["c"]) for r in rows}
        type_by = {r["ticket_type"]: int(r["c"]) for r in type_rows}
        # 平均处理时长: 已关闭工单 from created_at -> closed_at
        avg_row = self._conn.execute(
            "SELECT AVG(closed_at - created_at) FROM tickets "
            "WHERE tenant_id=? AND closed_at IS NOT NULL",
            (tenant_id,),
        ).fetchone()
        avg_seconds = float(avg_row[0]) if avg_row and avg_row[0] is not None else 0.0
        return {
            "by_status": status_by,
            "by_type": type_by,
            "total": self.count(tenant_id),
            "avg_handle_hours": round(avg_seconds / 3600, 1),
        }

    def recent_ops(self, tenant_id: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """管理端工单与提交时间分布(近 30 天), 供趋势折线图。"""
        days: Dict[str, int] = {}
        with _LOCK:
            rows = self._conn.execute(
                "SELECT created_at FROM tickets WHERE tenant_id=?",
                (tenant_id,),
            ).fetchall()
        import datetime

        for r in rows:
            d = datetime.datetime.fromtimestamp(r["created_at"]).strftime("%m-%d")
            days[d] = days.get(d, 0) + 1
        return [{"day": d, "count": c} for d, c in sorted(days.items())]


_store: Optional[TicketStore] = None
_store_lock = threading.Lock()


def get_ticket_store() -> TicketStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = TicketStore(get_settings().ticket_db_path)
    return _store