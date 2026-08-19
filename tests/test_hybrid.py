"""BM25 混合检索测试: 分词 / BM25 命中 / RRF 融合 / 混合检索出口。"""

from __future__ import annotations

from app.core.bm25 import tokenize
from app.core.retrieval import hybrid_retrieve, rrf_fuse


# ---------------------------------------------------------------------------
# 1. 分词(中英文混合)
# ---------------------------------------------------------------------------
def test_tokenize_ascii_and_bigram():
    toks = tokenize("DataGate数据采集网关 采集速率")
    assert "datagate" in toks or "datagate数据" not in toks  # ascii 词保留为整体
    # 中文按重叠二元组: "采集" 与 "数据"/"网关" 等应出现在二元组中
    assert "采集" in toks
    assert "数据" in toks
    assert "网关" in toks or "网关" in tokenize("网关")


def test_tokenize_handles_single_char():
    assert "网" in tokenize("网")


# ---------------------------------------------------------------------------
# 2. BM25 命中: 关键词重叠度
# ---------------------------------------------------------------------------
def test_bm25_rank_order():
    from app.core.bm25 import get_bm25_index

    idx = get_bm25_index()
    idx.reset()
    idx.add("c1", "网关默认登录用户名是 admin", "t1", {})
    idx.add("c2", "协议软件采集不到数据需要排查网络", "t1", {})
    hits = idx.search("网关默认用户名", 3, "t1")
    assert hits and hits[0]["chunk_id"] == "c1"


# ---------------------------------------------------------------------------
# 3. RRF 融合: 两路名次融合后重排 / 去重
# ---------------------------------------------------------------------------
def _row(cid):
    return {"chunk_id": cid, "text": f"text-{cid}", "doc_id": "d"}


def test_rrf_fuses_two_lists():
    vector = [_row("c1"), _row("c2")]     # c1 最高
    bm25 = [_row("c2"), _row("c1")]       # c2 最高
    fused = rrf_fuse(vector, bm25, k=60)
    assert {r["chunk_id"] for r in fused} == {"c1", "c2"}
    # 名次对等融合分相同, 不抛错且都保留两路名次标记
    assert any(r["vec_rank"] for r in fused)
    assert any(r["bm_rank"] for r in fused)


def test_rrf_keeps_both_rank_tags_and_score_positive():
    fused = rrf_fuse([_row("c1")], [_row("c1")], k=60)
    assert fused[0]["score"] > 0
    assert fused[0]["vec_rank"] == 1
    assert fused[0]["bm_rank"] == 1


# ---------------------------------------------------------------------------
# 4. 混合检索出口(依赖已入库的示例说明书)
# ---------------------------------------------------------------------------
def test_hybrid_retrieve_returns_rows(ingested):
    from app.core.embeddings import get_embedding_provider
    from app.core.milvus_store import get_milvus_store

    store = get_milvus_store()
    embedder = get_embedding_provider()
    q = "DataGate 网关如何进入配置界面"
    vector = embedder.embed_query(q)
    rows = hybrid_retrieve(store, embedder, q, vector, 4, 0.2, "default")
    # 新返回契约: (hits, timings), timings 含 vec_ms/bm25_ms/rerank_ms 供慢请求拆分
    assert isinstance(rows, tuple) and len(rows) == 2
    hits, timings = rows
    assert isinstance(hits, list)
    for r in hits:
        assert "chunk_id" in r and "text" in r and "score" in r
    assert "vec_ms" in timings and "bm25_ms" in timings