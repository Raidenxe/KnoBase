"""全链路追踪: trace_id 生成、span 记录、观测查询接口数据源。

- 每次请求(聊天/检索)产生一个 trace_id; 各阶段 span 以内存环形缓冲记录
- 可选落库 trace.db(用于持久化观测); 过期自动清理
- 提供 metrics: 各阶段 P50/P95/峰值, 检索条数, 拒答/失败率
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

from app.config import get_settings

_MAX_TRACES = 2000


class TraceStore:
    def __init__(self, db_path: str) -> None:
        self._mem: Deque[Dict[str, Any]] = deque(maxlen=_MAX_TRACES)
        self._lock = threading.Lock()
        self._db_path = db_path or ""
        if db_path:
            from pathlib import Path

            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            try:
                self._conn = sqlite3.connect(db_path, check_same_thread=False)
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS traces("
                    " trace_id TEXT PRIMARY KEY, created_at REAL, spans TEXT, status TEXT)"
                )
                self._conn.commit()
            except Exception:  # noqa: BLE001
                self._conn = None
        else:
            self._conn = None

    # ------------------------------------------------------------------
    def start(self, kind: str = "chat", tenant_id: str = "") -> str:
        trace_id = uuid.uuid4().hex[:16]
        record = {
            "trace_id": trace_id,
            "kind": kind,
            "tenant_id": tenant_id,
            "spans": [],          # [{stage, start_ms, duration_ms}]
            "started_at": time.time(),
            "ended_at": None,
            "status": "running",
        }
        with self._lock:
            self._mem.append(record)
        return trace_id

    def add_span(self, trace_id: str, stage: str, duration_ms: int,
                 detail: Optional[Dict[str, Any]] = None) -> None:
        span = {"stage": stage, "duration_ms": duration_ms}
        if detail:
            span["detail"] = detail
        with self._lock:
            rec = next((r for r in self._mem if r["trace_id"] == trace_id), None)
            if rec:
                rec["spans"].append(span)

    def finish(self, trace_id: str, status: str = "ok") -> None:
        with self._lock:
            rec = next((r for r in self._mem if r["trace_id"] == trace_id), None)
            if rec:
                rec["status"] = status
                rec["ended_at"] = time.time()
        self._persist(trace_id)

    def _persist(self, trace_id: str) -> None:
        if not self._conn:
            return
        try:
            with self._lock:
                rec = next((r for r in self._mem if r["trace_id"] == trace_id), None)
            import json

            self._conn.execute(
                "INSERT OR REPLACE INTO traces(trace_id,created_at,spans,status)"
                " VALUES(?,?,?,?)",
                (trace_id, rec["started_at"], json.dumps(rec["spans"]), rec["status"]),
            )
            self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def get(self, trace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            rec = next((r for r in self._mem if r["trace_id"] == trace_id), None)
        if rec:
            return dict(rec)
        if self._conn:
            try:
                import json

                row = self._conn.execute(
                    "SELECT * FROM traces WHERE trace_id=?", (trace_id,)
                ).fetchone()
                if row:
                    return {"trace_id": row[0], "created_at": row[1],
                            "spans": json.loads(row[2]), "status": row[3]}
            except Exception:  # noqa: BLE001
                pass
        return None

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._mem)[-limit:][::-1]
        return items

    # ------------------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            records = [r for r in self._mem
                       if now - r["started_at"] < get_settings().trace_ttl_seconds]
        if not records:
            return {"requests": 0}
        by_stage: Dict[str, List[float]] = defaultdict(list)
        failures = 0
        total_retrieved = 0
        for r in records:
            if r["status"] != "ok":
                failures += 1
            for s in r["spans"]:
                by_stage[s["stage"]].append(s["duration_ms"])
            retr = next((s for s in r["spans"] if s["stage"] == "retrieve"), None)
            if retr:
                total_retrieved += 1
        stage_stats = {}
        for stage, durs in by_stage.items():
            durs.sort()
            n = len(durs)
            stage_stats[stage] = {
                "count": n,
                "p50_ms": int(durs[n // 2]),
                "p95_ms": int(durs[min(int(n * 0.95), n - 1)]),
                "max_ms": int(durs[-1]),
            }
        total = len(records)
        return {
            "requests": total,
            "failure_rate": round(failures / total, 4) if total else 0.0,
            "stages": stage_stats,
        }


_store: Optional[TraceStore] = None


def get_trace_store() -> TraceStore:
    global _store
    if _store is None:
        _store = TraceStore(get_settings().trace_db_path)
    return _store


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]