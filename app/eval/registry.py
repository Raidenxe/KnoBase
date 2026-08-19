"""评估指标计算(检索/端到端): Hits@k / Precision@k / Recall@k / MRR@k。

均为纯函数, 便于单元测试。gold 以 doc_id 或 chunk_id 集合表示,
检索结果视为有序列表, 依据其命中情况计算各级指标。
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Set


def _recall_list(result: List[Dict], gold: Set[str], key: str = "doc_id") -> bool:
    return any(gold.intersection({r.get(key) for r in result}))


def hits_at_k(
    result: List[Dict], gold: Set[str], k: int = 1, key: str = "doc_id"
) -> float:
    """Hits@k: 前 k 个结果是否含至少一个 gold 命中(0/1)。"""
    return 1.0 if _recall_list(result[:k], gold, key) else 0.0


def precision_at_k(
    result: List[Dict], gold: Set[str], k: int = 1, key: str = "doc_id"
) -> float:
    """Precision@k: 前 k 个结果中命中的占比。"""
    top = result[:k]
    if not top:
        return 0.0
    hits = sum(1 for r in top if gold.intersection({r.get(key)}))
    return hits / len(top)


def recall_at_k(
    result: List[Dict], gold: Set[str], k: int = 1, key: str = "doc_id"
) -> float:
    """Recall@k: 前 k 个结果命中的 gold 项数 / 全部 gold 项数。"""
    top_keys: Set[str] = {r.get(key) for r in result[:k] if r.get(key)}
    if not gold:
        return 0.0
    return len(top_keys & gold) / len(gold)


def mrr_at_k(
    result: List[Dict], gold: Set[str], k: int = 1, key: str = "doc_id"
) -> float:
    """MRR@k: 首个 gold 命中所处名次倒数的均值(对单条结果即 1/rank)。"""
    for rank, r in enumerate(result[:k], start=1):
        if gold.intersection({r.get(key)}):
            return 1.0 / rank
    return 0.0


# ---------------------------------------------------------------------------
# 批量聚合
# ---------------------------------------------------------------------------
def aggregate_retrieval_metrics(
    cases: List[Dict],  # [{result: [...], gold: set|list]]
    ks: Iterable[int] = (1, 3, 5),
    key: str = "doc_id",
) -> Dict[str, float]:
    ks = list(ks)
    agg = {m: {k: 0.0 for k in ks} for m in ("hits", "precision", "recall", "mrr")}
    n = len(cases) or 1
    for c in cases:
        result = c["result"]
        gold = set(c["gold"])
        for k in ks:
            agg["hits"][k] += hits_at_k(result, gold, k, key)
            agg["precision"][k] += precision_at_k(result, gold, k, key)
            agg["recall"][k] += recall_at_k(result, gold, k, key)
            agg["mrr"][k] += mrr_at_k(result, gold, k, key)
    return {
        f"{m.title()}@{k}": round(agg[m][k] / n, 4)
        for m in ("hits", "precision", "recall", "mrr")
        for k in ks
    }