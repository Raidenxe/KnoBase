"""LLM 调用日志: 记录每次真实大模型调用的 Token 消耗, 用于成本核算。

假定价目表(config 内可覆盖), 汇总按 模型/时间段 输出 token 总量与估算费用。
默认 (dist) 演示环境 provider=mock, 不产生真实计费, 表保持为空(如实反映)。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_settings

# 每百万 token 计费(美元) 与 汇率示例; 可被评测脚本/手写调整
_MODEL_PRICE_PER_1M = {"*": 1.2}


class LLMUsageStore:
    def __init__(self, db_path: str, price_per_1m: Optional[Dict[str, float]] = None) -> None:
        self._db_path = db_path
        self._prices = price_per_1m or _MODEL_PRICE_PER_1M
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_usage("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts REAL, model TEXT, provider TEXT,"
            " prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,"
            " request_id TEXT)"
        )
        self._conn.commit()
        self._lock = threading.Lock()

    def record(self, model: str, provider: str, prompt_tokens: int,
               completion_tokens: int, request_id: str = "") -> None:
        prompt_tokens = max(int(prompt_tokens or 0), 0)
        completion_tokens = max(int(completion_tokens or 0), 0)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO llm_usage(ts,model,provider,prompt_tokens,completion_tokens,"
                    " total_tokens,request_id) VALUES(?,?,?,?,?,?,?)",
                    (time.time(), model or "unknown", provider or "",
                     prompt_tokens, completion_tokens,
                     prompt_tokens + completion_tokens, request_id or ""),
                )
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def summary(self, hours: int = 24) -> Dict[str, Any]:
        since = time.time() - hours * 3600
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT model,provider,SUM(prompt_tokens),SUM(completion_tokens),"
                    " SUM(total_tokens),COUNT(*) FROM llm_usage"
                    " WHERE ts>=? GROUP BY model,provider", (since,)
                ).fetchall()
        except Exception:  # noqa: BLE001
            rows = []
        by_model = []
        totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        cost_total = 0.0
        for (model, provider, pr, co, to, calls) in rows:
            price = self._prices.get(model, self._prices.get("*", 0.0))
            cost = (float(pr) + float(co)) / 1_000_000 * price
            cost_total += cost
            totals["prompt_tokens"] += int(pr)
            totals["completion_tokens"] += int(co)
            totals["total_tokens"] += int(to)
            totals["calls"] += int(calls)
            by_model.append({
                "model": model, "provider": provider or "",
                "calls": int(calls), "prompt_tokens": int(pr),
                "completion_tokens": int(co), "total_tokens": int(to),
                "estimated_cost": round(cost, 6),
            })
        by_model.sort(key=lambda r: -r["total_tokens"])
        return {
            "hours": hours,
            "totals": totals,
            "estimated_cost": round(cost_total, 6),
            "by_model": by_model,
        }


_store: Optional[LLMUsageStore] = None


def get_llm_usage_store() -> LLMUsageStore:
    global _store
    if _store is None:
        _store = LLMUsageStore(get_settings().llm_usage_db_path)
    return _store