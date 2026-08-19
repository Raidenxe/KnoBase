"""评估指标计算测试: Hits@k / Precision@k / Recall@k / MRR@k / 聚合。"""

from __future__ import annotations

from app.eval.registry import (
    aggregate_retrieval_metrics,
    hits_at_k,
    mrr_at_k,
    precision_at_k,
    recall_at_k,
)


def _rows(*ids):
    return [{"doc_id": i} for i in ids]


def test_hits_at_k():
    assert hits_at_k(_rows("a", "b"), {"b"}, 1) == 0.0   # 前1名未命中
    assert hits_at_k(_rows("a", "b"), {"b"}, 2) == 1.0


def test_precision_at_k():
    assert precision_at_k(_rows("a", "b"), {"a"}, 2) == 0.5
    assert precision_at_k(_rows("a", "b"), {"c"}, 2) == 0.0


def test_recall_at_k():
    assert recall_at_k(_rows("a", "b"), {"b", "c"}, 2) == 0.5
    assert recall_at_k(_rows("a", "b", "c"), {"b", "c"}, 3) == 1.0


def test_mrr_at_k():
    assert mrr_at_k(_rows("x", "a"), {"a"}, 2) == 0.5
    assert mrr_at_k(_rows("x", "y"), {"a"}, 2) == 0.0


def test_aggregate():
    cases = [
        {"result": _rows("a", "b"), "gold": ["b"]},
        {"result": _rows("c"), "gold": ["c"]},
    ]
    out = aggregate_retrieval_metrics(cases, ks=(1, 2))
    assert 0.0 < out["Precision@1"] <= 1.0
    assert 0.0 < out["Hits@2"] <= 1.0
    assert out["Mrr@1"] == 0.5