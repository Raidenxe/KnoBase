"""慢请求明细存储: 记录全程耗时超过阈值的请求, 供管理面板取用。

由 TracingMiddleware 在请求结束时调用 record(); 该处同时注入 trace_id,
前端可据此通过 GET /traces/{id}/detail 展开单请求的阶段拆分。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

from app.config import get_settings

_MAX = 200


class SlowRequestStore:
    def __init__(self, maxlen: int = _MAX) -> None:
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._threshold = get_settings().slow_request_threshold_ms

    @property
    def threshold_ms(self) -> int:
        return self._threshold

    def record(self, trace_id: str, path: str, total_ms: int,
               status: str = "ok", started_at: Optional[float] = None) -> None:
        if total_ms < self._threshold:
            return
        with self._lock:
            self._buf.appendleft({
                "trace_id": trace_id,
                "path": path,
                "total_ms": total_ms,
                "status": status,
                "created_at": started_at or time.time(),
                "threshold_ms": self._threshold,
            })

    def recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buf)[: max(limit, 0)]


_store: Optional[SlowRequestStore] = None
_store_lock = threading.Lock()


def get_slow_request_store() -> SlowRequestStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = SlowRequestStore()
    return _store