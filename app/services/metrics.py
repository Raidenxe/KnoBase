"""接口性能监控: 按路由统计延迟分位(P50/P95/P99)与 QPS。

由 HTTP 中间件埋点写入(仅 /api/* 路径), 零阻塞关键路径:
- 内存环形缓冲保留最近 N 条采样, 过期自动清理(窗口 = endpoint_metrics_ttl 秒)
- 仅按 路径 去参聚合, 不区分方法/租户, 用于运维概览
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, List, Optional

from app.config import get_settings

_MAX_SAMPLES = 5000


class EndpointMetrics:
    def __init__(self, max_samples: int = _MAX_SAMPLES) -> None:
        self._samples: Deque[tuple] = deque(maxlen=max_samples)  # (ts, path, duration_ms)
        self._lock = threading.Lock()

    def record(self, path: str, duration_ms: int) -> None:
        if not path:
            return
        p = path.split("?", 1)[0]
        p = "/" + p.lstrip("/")
        with self._lock:
            self._samples.append((time.time(), p, duration_ms))

    def stats(self, ttl: int = 0) -> Dict[str, Any]:
        ttl = ttl or get_settings().endpoint_metrics_ttl
        now = time.time()
        with self._lock:
            samples = [s for s in self._samples if now - s[0] < ttl]
        if not samples:
            return {"total_requests": 0, "endpoints": []}
        by_path: Dict[str, List[int]] = defaultdict(list)
        for _ts, path, ms in samples:
            by_path[path].append(ms)
        total = len(samples)
        window = max(ttl, 1) / 60.0  # 分钟
        rows = []
        for path, durs in by_path.items():
            durs.sort()
            n = len(durs)
            rows.append({
                "path": path,
                "count": n,
                "qps": round(n / max(window * 60, 1), 3),
                "avg_ms": int(sum(durs) / n),
                "p50_ms": int(durs[n // 2]),
                "p95_ms": int(durs[min(int(n * 0.95), n - 1)]),
                "p99_ms": int(durs[min(int(n * 0.99), n - 1)]),
                "max_ms": int(durs[-1]),
            })
        rows.sort(key=lambda r: -r["count"])
        return {"total_requests": total, "window_seconds": ttl, "endpoints": rows}


_store: Optional[EndpointMetrics] = None


def get_endpoint_metrics() -> EndpointMetrics:
    global _store
    if _store is None:
        _store = EndpointMetrics()
    return _store