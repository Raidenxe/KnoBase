"""多路混合检索: 向量检索(COSINE) + BM25 关键词检索, RRF 融合。

外部调用入口 `hybrid_retrieve(...)`, 返回与向量检索一致的 chunk 行结构
(含 text/doc_id/… 及 score=RRF 融合分), 供 LangGraph retrieve 节点使用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.bm25 import get_bm25_index
from app.core.rerank import rerank_if_enabled


def rrf_fuse(
    vector_rows: List[Dict[str, Any]],
    bm25_rows: List[Dict[str, Any]],
    k: Optional[int] = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Reciprocal Rank Fusion: 按名次倒加权求和, 兼顾两路召回。

    返回按融合分降序排列的行列表, 每个 chunk 附原始两路名次与 score。
    """
    from app.core.settings_rt import effective_settings

    settings = effective_settings()
    k = k if k is not None else settings.rrf_k
    vector_weight = vector_weight if vector_weight is not None else settings.rrf_vector_weight
    bm25_weight = bm25_weight if bm25_weight is not None else settings.rrf_bm25_weight

    acc: Dict[str, Dict[str, Any]] = {}
    for rows, w, tag in (
        (vector_rows, vector_weight, "vec"),
        (bm25_rows, bm25_weight, "bm"),
    ):
        for rank, row in enumerate(rows, start=1):
            cid = row.get("chunk_id")
            if not cid:
                continue
            e = acc.setdefault(cid, {"row": None, "value": 0.0, "vec_rank": None,
                                     "bm_rank": None})
            e["value"] += w / (k + rank)
            e["row"] = e["row"] if e["row"] is not None else row
            e[f"{tag}_rank"] = rank

    fused: List[Dict[str, Any]] = []
    for cid, e in acc.items():
        row = dict(e["row"] or {})
        row["chunk_id"] = cid
        row["score"] = round(e["value"], 4)
        row["vec_rank"] = e["vec_rank"]
        row["bm_rank"] = e["bm_rank"]
        fused.append(row)
    fused.sort(key=lambda r: -r["score"])
    return fused


def hybrid_retrieve(
    store,
    embedder,
    query_text: str,
    query_vector: List[float],
    top_k: int,
    score_threshold: float,
    tenant_id: Optional[str] = None,
    doc_version: Optional[int] = None,
):
    """向量 + BM25 并行召回 → RRF 融合(尚未去重/词法精排)。

    - 向量路: 超采样 2×, 按 score_threshold 过滤
    - BM25 路: 取 settings.hybrid_bm25_k 条
    - doc_version: 非 None 时两路均按目标版本文档过滤
    - 末尾: 若启用 rerank, 对融合结果做精排裁剪
    - 返回 (rows, timings): timings 记录 vec_ms/bm25_ms/rerank_ms, 供慢请求拆分
    """
    import time
    from app.core.settings_rt import effective_settings

    settings = effective_settings()
    bm25 = get_bm25_index()
    timings: Dict[str, Any] = {}

    t0 = time.perf_counter()
    vec_rows = store.search(
        query_vector, settings.hybrid_bm25_k * 2, None, score_threshold, tenant_id,
        doc_version,
    )
    timings["vec_ms"] = int((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    try:
        bm25_rows = bm25.search(query_text, settings.hybrid_bm25_k, tenant_id, doc_version)
        timings["bm25_ms"] = int((time.perf_counter() - t0) * 1000)
    except Exception:  # noqa: BLE001 — BM25 不可用时自动降级为纯向量
        bm25_rows = []
        timings["bm25_ms"] = -1

    fused = rrf_fuse(vec_rows, bm25_rows)

    # 去重(网页类文档常含重复片段), 释放候选名额
    seen, unique = set(), []
    for r in fused:
        key = (r.get("text") or "")[:200]
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique = unique[:top_k]

    # 可选 Rerank 精排(升配为 job score), 未启用/不可用则保持原序
    t0 = time.perf_counter()
    reranked = rerank_if_enabled(query_text, unique, settings)
    timings["rerank_ms"] = int((time.perf_counter() - t0) * 1000) if settings.rerank_enabled else -1
    for r in reranked:
        # 精排复用 rerank_score 作为最终排序分, 供上层使用
        if "rerank_score" in r:
            r["score"] = round(r["rerank_score"], 4)
    hits = reranked if settings.rerank_enabled else unique[:top_k]
    return hits, timings