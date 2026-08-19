"""每文档命中统计(SQLite): 记录哪些文档被回答引用(命中), 用于知识库覆盖度分析。

- 命中: 某次回答的 citations 中出现该 doc_id, 则 hit_count +1
- 支持按租户隔离查询; 提供最近命中时间便于区分"热点"文档
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DocStatsStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS doc_hits(
                    doc_id TEXT,
                    doc_name TEXT DEFAULT '',
                    tenant_id TEXT DEFAULT '',
                    hit_count INTEGER DEFAULT 0,
                    last_hit_at TEXT,
                    PRIMARY KEY(doc_id, tenant_id)
                );
                """
            )
            self._conn.commit()

    def record_hits(
        self, doc_ids: List[str], doc_names: Dict[str, str], tenant_id: str
    ) -> None:
        """对本次回答命中的文档, hit_count +1。幂等去重(同一回答中重复引用只计一次)。"""
        if not doc_ids:
            return
        ts = _now()
        with _LOCK:
            for doc_id in {d for d in doc_ids if d}:
                name = doc_names.get(doc_id, "")
                self._conn.execute(
                    "INSERT INTO doc_hits(doc_id,doc_name,tenant_id,hit_count,last_hit_at)"
                    " VALUES(?,?,?,1,?)"
                    " ON CONFLICT(doc_id,tenant_id) DO UPDATE SET"
                    " hit_count=hit_count+1, last_hit_at=excluded.last_hit_at,"
                    " doc_name=excluded.doc_name",
                    (doc_id, name[:500], tenant_id, ts),
                )
            self._conn.commit()

    def stats(self, tenant_id: str = "") -> List[Dict[str, object]]:
        extra, args = (" WHERE tenant_id=?", (tenant_id,)) if tenant_id else ("", ())
        with _LOCK:
            rows = self._conn.execute(
                f"SELECT doc_id, doc_name, hit_count, last_hit_at FROM doc_hits"
                f"{extra} ORDER BY hit_count DESC, last_hit_at DESC",
                args,
            ).fetchall()
        return [dict(r) for r in rows]

    def hits_map(self, tenant_id: str = "") -> Dict[str, Dict[str, object]]:
        return {r["doc_id"]: r for r in self.stats(tenant_id)}


_doc_stats_store: Optional[DocStatsStore] = None


def get_doc_stats_store() -> DocStatsStore:
    global _doc_stats_store
    if _doc_stats_store is None:
        from app.config import get_settings

        _doc_stats_store = DocStatsStore(get_settings().doc_stats_db_path)
    return _doc_stats_store