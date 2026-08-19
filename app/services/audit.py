"""操作审计日志: 记录敏感操作(谁在什么时间做了什么)。

仅记录高风险/敏感动作(如上传、删除、导入、分享、改配置等), 落盘到
data/audit.db, 供管理员在后台查看, 支撑责任追溯。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_settings

_MAX_RECENT = 1000


class AuditStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS audit("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL, actor TEXT, actor_role TEXT, action TEXT,"
            " target TEXT, tenant_id TEXT, detail TEXT)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def record(
        self,
        action: str,
        actor: str = "",
        target: str = "",
        tenant_id: str = "",
        detail: str = "",
        role: str = "",
    ) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit(ts,actor,actor_role,action,target,tenant_id,detail)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (time.time(), actor or "anonymous", role, action,
                     target or "", tenant_id or "", detail or ""),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT ts,actor,actor_role,action,target,tenant_id,detail"
                    " FROM audit ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [self._row_dict(r) for r in rows]

    def recent_for(self, target: str, limit: int = 100) -> List[Dict[str, Any]]:
        """按操作对象(doc_id/文件)过滤的审计时间线, 用于文档详情页。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT ts,actor,actor_role,action,target,tenant_id,detail"
                    " FROM audit WHERE target=? ORDER BY id DESC LIMIT ?",
                    (target or "", limit),
                ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [self._row_dict(r) for r in rows]

    @staticmethod
    def _row_dict(row) -> Dict[str, Any]:
        (ts, actor, role, action, target, tenant, detail) = row
        return {
            "ts": ts, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "actor": actor, "role": role or "", "action": action,
            "target": target, "tenant_id": tenant or "", "detail": detail or "",
        }


_store: Optional[AuditStore] = None


def get_audit_store() -> AuditStore:
    global _store
    if _store is None:
        _store = AuditStore(get_settings().audit_db_path)
    return _store


def audit(action: str, *, actor: str = "", target: str = "", detail: str = "",
          tenant_id: str = "", role: str = "") -> None:
    """便捷入口: audit("upload", actor="admin", target="a.md", tenant_id="default")"""
    try:
        get_audit_store().record(action, actor, target, tenant_id, detail, role)
    except Exception:  # noqa: BLE001
        pass