"""资源水位监控: CPU / 内存 / 磁盘 24h 曲线采集与查询。

- 后台守护线程按 `resource_sample_interval`(默认 60s)用 psutil 采样,
  每次样本同时写入 SQLite(resource.db) 与内存环形缓冲(最近 24h)。
- 提供即时读数(current)与聚合曲线(series)两个查询入口, 供管理接口使用。
- psutil 不可用(未安装)时降级: current 返回不可用标记, series 返回空。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

_MAX_BUFFER = 1440  # 24h @ 60s = 1440 点 (内存环形缓冲)


class ResourceMonitor:
    def __init__(
        self,
        db_path: str,
        interval: int,
        buffer_size: int = _MAX_BUFFER,
    ) -> None:
        self._interval = max(interval or 60, 10)
        self._lock = threading.Lock()
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=buffer_size)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._available = self._probe_psutil()
        self._conn: Optional[sqlite3.Connection] = None
        if db_path and self._available:
            try:
                from pathlib import Path

                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                self._conn = sqlite3.connect(db_path, check_same_thread=False)
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS samples("
                    " ts REAL PRIMARY KEY, cpu REAL, mem_used REAL,"
                    " mem_total REAL, disk_used REAL, disk_total REAL, rss REAL)"
                )
                # 只保留最近 72h, 防表无限膨胀
                self._conn.execute(
                    "DELETE FROM samples WHERE ts < ?", (time.time() - 72 * 3600,)
                )
                self._conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.warning("资源采样落库不可用: %s", exc)
                self._conn = None

    @staticmethod
    def _probe_psutil() -> bool:
        try:
            import psutil  # noqa: F401

            psutil.cpu_percent(interval=0)
            return True
        except Exception:  # noqa: BLE001
            logger.warning("psutil 未安装或不可用, 资源监控降级为不可用")
            return False

    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="resource-monitor"
        )
        self._thread.start()
        logger.info("资源监控线程已启动(interval=%ds)", self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._conn:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        import psutil

        while not self._stop.is_set():
            # cpu_percent(interval=...) 阻塞采样窗, 返回该窗口内平均使用率
            cpu = psutil.cpu_percent(interval=self._interval)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            try:
                rss = psutil.Process().memory_info().rss
            except Exception:  # noqa: BLE001
                rss = 0
            sample = {
                "ts": time.time(),
                "cpu": round(cpu, 1),
                "mem_used": mem.used,
                "mem_total": mem.total,
                "disk_used": disk.used,
                "disk_total": disk.total,
                "rss": int(rss),
            }
            with self._lock:
                self._buf.append(sample)
                if self._conn:
                    try:
                        self._conn.execute(
                            "INSERT OR REPLACE INTO samples(ts,cpu,mem_used,mem_total,"
                            "disk_used,disk_total,rss) VALUES(?,?,?,?,?,?,?)",
                            (sample["ts"], sample["cpu"], sample["mem_used"],
                             sample["mem_total"], sample["disk_used"],
                             sample["disk_total"], sample["rss"]),
                        )
                        self._conn.commit()
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------------
    def current(self) -> Dict[str, Any]:
        if not self._available:
            return {"available": False, "message": "psutil 不可用"}
        import psutil

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        try:
            cpu = psutil.cpu_percent(interval=0.5)
        except Exception:  # noqa: BLE001
            cpu = 0.0
        try:
            rss = psutil.Process().memory_info().rss
        except Exception:  # noqa: BLE001
            rss = 0
        return {
            "available": True,
            "cpu": round(cpu, 1),
            "mem_used": mem.used,
            "mem_total": mem.total,
            "mem_percent": round(mem.percent, 1),
            "disk_used": disk.used,
            "disk_total": disk.total,
            "disk_percent": round(disk.percent, 1),
            "rss": int(rss),
            "ts": time.time(),
        }

    def series(self, hours: int = 24, step: int = 60) -> Dict[str, Any]:
        """返回聚合曲线数据点。优先读内存缓冲, 缓冲不足时回退 SQLite。"""
        if not self._available:
            return {"available": False, "points": []}
        step = max(step, 10)
        since = time.time() - hours * 3600
        with self._lock:
            rows = list(self._buf)
        # 内存缓冲只保留最近 1440 点, 可能不足 hours -> 兜底查询落库
        points = [r for r in rows if r["ts"] >= since] if rows else []
        if len(points) < 2 and self._conn:
            try:
                cur = self._conn.execute(
                    "SELECT ts,cpu,mem_used,mem_total,disk_used,disk_total "
                    "FROM samples WHERE ts >= ? ORDER BY ts",
                    (since,),
                )
                points = [
                    {"ts": t, "cpu": c, "mem_used": mu, "mem_total": mt,
                     "disk_used": du, "disk_total": dt}
                    for t, c, mu, mt, du, dt, *_ in cur.fetchall()
                ]
            except Exception:  # noqa: BLE001
                points = []
        # 按 step 二次聚合(取区间均值)
        buckets: Dict[int, Dict[str, Any]] = {}
        for p in points:
            b = int(p["ts"] // step) * step
            acc = buckets.setdefault(b, {"ts": b, "n": 0, "cpu_sum": 0.0,
                                         "mem_used": 0, "mem_total": 0})
            acc["n"] += 1
            acc["cpu_sum"] += p["cpu"]
            acc["mem_used"] = max(acc["mem_used"], p["mem_used"])
            acc["mem_total"] = p["mem_total"]
        out = []
        for b in sorted(buckets):
            acc = buckets[b]
            out.append({
                "ts": acc["ts"],
                "cpu": round(acc["cpu_sum"] / acc["n"], 1),
                "mem_used": acc["mem_used"],
                "mem_total": acc["mem_total"],
                "disk_used": acc.get("disk_used"),
                "disk_total": acc.get("disk_total"),
            })
        return {"available": True, "points": out, "step": step, "hours": hours}


_monitor: Optional[ResourceMonitor] = None
_monitor_lock = threading.Lock()


def get_resource_monitor() -> ResourceMonitor:
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                s = get_settings()
                _monitor = ResourceMonitor(s.resource_db_path, s.resource_sample_interval)
    return _monitor


def start_resource_monitor() -> None:
    get_resource_monitor().start()