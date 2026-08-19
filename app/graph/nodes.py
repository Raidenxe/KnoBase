"""LangGraph 对话图节点: 问题分析 → 检索 → 相关性精筛 → 受限生成 → 一致性校验。

所有节点通过 state["emit"] 异步推送 SSE 事件(token/status/citations/reset),
实现"边推理边流式返回"; 条件路由实现防幻觉闭环:
    无相关资料 → 拒答(不编造)
    校验失败   → 带反馈重试 → 仍失败 → 安全拒答
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List

from app.config import get_settings
from app.core.embeddings import get_embedding_provider
from app.core.llm import get_llm_service, split_sentences
from app.core.milvus_store import get_milvus_store
from app.core.retrieval import hybrid_retrieve
from app.graph.state import GraphState
from app.services.trace_store import get_trace_store

logger = logging.getLogger(__name__)

_CITE = re.compile(r"\[(\d+)\]")


def _span(state: GraphState, stage: str, duration_ms: int, detail: Dict[str, Any] | None = None) -> None:
    trace_id = state.get("trace_id")
    if trace_id:
        get_trace_store().add_span(trace_id, stage, duration_ms, detail)

# 混合重排: 余弦相似度为主, 叠加问题关键词命中率加成
_QUERY_STOPWORDS = {
    "什么", "是什么", "怎么", "怎么样", "如何", "哪些", "哪个", "为什么", "请问",
    "一下", "介绍", "说明", "告诉", "还有", "以及", "支持", "指的", "指",
}


def _lexical_hit(question: str, text: str) -> float:
    terms = [t for t in re.findall(r"[A-Za-z][A-Za-z0-9_.-]+|[\u4e00-\u9fff]{2,4}", question.lower()) if t not in _QUERY_STOPWORDS]
    if not terms:
        return 0.0
    low = text.lower()
    return sum(1 for t in terms if t in low) / len(terms)


REFUSAL_TEMPLATE = (
    "抱歉，根据现有产品说明书资料，暂时无法回答该问题。\n\n"
    "原因：{reason}\n\n"
    "建议：\n"
    "1. 尝试换一种问法，或指明具体的产品名称与模块；\n"
    "2. 联系人工维保支持热线 400-800-1234（7×24 小时）获取帮助。"
)


async def _emit(state: GraphState, event: str, data: Dict[str, Any]) -> None:
    emit = state.get("emit")
    if emit:
        try:
            await emit({"event": event, "data": data})
        except Exception:  # noqa: BLE001
            logger.exception("SSE 事件推送失败")


# ---------------------------------------------------------------------------
# 1. 问题分析: 多轮指代消解
# ---------------------------------------------------------------------------
async def analyze_query(state: GraphState) -> Dict[str, Any]:
    settings = get_settings()
    llm = get_llm_service()
    question = state["question"]
    history = state.get("history", [])
    await _emit(state, "status", {"stage": "analyze", "message": "正在理解问题..."})
    t0 = time.perf_counter()
    standalone = await llm.condense_question(question, history)
    ms = int((time.perf_counter() - t0) * 1000)
    _span(state, "analyze", ms)
    logger.info("[analyze] 原问题: %s → 独立问题: %s", question, standalone)
    return {"standalone_question": standalone, "retries": 0,
            "metrics": {"analyze_ms": ms}}


# ---------------------------------------------------------------------------
# 2. 多路混合检索(向量 + BM25, RRF 融合)
# ---------------------------------------------------------------------------
async def retrieve(state: GraphState) -> Dict[str, Any]:
    # 读取动态配置层(可热覆盖 Top-K / 阈值 / RRF / Rerank, 无需重启)
    from app.core.settings_rt import effective_settings

    settings = effective_settings()
    embedder = get_embedding_provider()
    store = get_milvus_store()
    tenant_id = state.get("tenant_id") or get_settings().default_tenant
    doc_version = state.get("doc_version")
    await _emit(state, "status", {"stage": "retrieve", "message": "正在检索产品说明书..."})
    t0 = time.perf_counter()
    vector = await asyncio.to_thread(embedder.embed_query, state["standalone_question"])
    hint = f" (版本 v{doc_version})" if doc_version else ""
    retr = await asyncio.to_thread(
        hybrid_retrieve,
        store,
        embedder,
        state["standalone_question"],
        vector,
        settings.retrieval_top_k,
        settings.retrieval_score_threshold,
        tenant_id,
        doc_version,
    )
    # hybrid_retrieve 返回 (hits, timings); 兼容仅返回 hits 的旧实现
    if isinstance(retr, tuple):
        hits, timings = retr
    else:
        hits, timings = retr, {}
    # 分类授权后置过滤(生产模式按用户组可读分类收敛; demo/未授权时为 None 不过滤)
    readable = state.get("category_scope")  # None(不限) | 可读分类名列表
    if readable is not None and hits:
        from app.core.access import filter_hits_by_scope
        from app.services.doc_meta import get_doc_meta_store

        meta = get_doc_meta_store().get_many_meta(
            [h.get("doc_id", "") for h in hits], tenant_id
        )
        hits = filter_hits_by_scope(hits, readable, meta)
    # 词法精排: 同领域笔记库中纯语义排序易被泛领域块淹没, 叠加关键词命中提升精确块排名
    q = state["standalone_question"]
    hits.sort(key=lambda h: -(h["score"] + 0.15 * _lexical_hit(q, h["text"])))
    ms = int((time.perf_counter() - t0) * 1000)
    _span(state, "retrieve", ms, detail={
        "hits": len(hits),
        "top_k": settings.retrieval_top_k,
        "threshold": settings.retrieval_score_threshold,
        **(timings or {}),
    })
    await _emit(state, "status", {"stage": "retrieved", "message": f"召回 {len(hits)} 个相关片段", "retrieval_ms": ms})
    logger.info("[retrieve] %d hits in %d ms (阈值=%.2f)", len(hits), ms, settings.retrieval_score_threshold)
    return {"retrieved": hits, "metrics": {**state.get("metrics", {}), "retrieval_ms": ms}}


# ---------------------------------------------------------------------------
# 3. 相关性精筛(防幻觉·第一道闸): 向量阈值 + 语义/关键词双重判定
# ---------------------------------------------------------------------------
async def grade_documents(state: GraphState) -> Dict[str, Any]:
    settings = get_settings()
    llm = get_llm_service()
    question = state["standalone_question"]
    hits = state.get("retrieved", [])
    await _emit(state, "status", {"stage": "grade", "message": "正在评估片段相关性..."})
    t0 = time.perf_counter()

    async def grade(hit: Dict[str, Any]) -> bool:
        try:
            return await llm.grade_relevance(question, hit["text"], hit["score"])
        except Exception:  # noqa: BLE001
            return False

    verdicts = await asyncio.gather(*(grade(h) for h in hits)) if hits else []
    filtered = [h for h, ok in zip(hits, verdicts) if ok][: settings.max_context_chunks]
    gms = int((time.perf_counter() - t0) * 1000)
    _span(state, "grade", gms)
    if not filtered:
        logger.info("[grade] 无相关片段, 进入拒答分支")
        return {
            "filtered": [],
            "route": "refuse",
            "refusal_reason": "未在知识库中检索到与该问题直接相关的内容",
            "metrics": {**state.get("metrics", {}), "grade_ms": gms},
        }
    logger.info("[grade] %d/%d 片段通过相关性判定", len(filtered), len(hits))
    return {"filtered": filtered, "route": "generate",
            "metrics": {**state.get("metrics", {}), "grade_ms": gms}}


# ---------------------------------------------------------------------------
# 4. 受限生成(流式, 强制引用来源)
# ---------------------------------------------------------------------------
async def generate(state: GraphState) -> Dict[str, Any]:
    llm = get_llm_service()
    filtered = state["filtered"]
    blocks: List[Dict[str, Any]] = []
    for i, h in enumerate(filtered, start=1):
        blocks.append(
            {
                "index": i,
                "chunk_id": h["chunk_id"],
                "doc_id": h["doc_id"],
                "doc_name": h["doc_name"],
                "section_path": h["section_path"],
                "page": h.get("page", -1),
                "score": h["score"],
                "text": h["text"],
            }
        )

    if state.get("retries", 0) > 0:
        await _emit(state, "reset", {"reason": "一致性校验未通过，正在生成更严谨的回答..."})

    await _emit(state, "status", {"stage": "generate", "message": "正在生成回答..."})
    t0 = time.perf_counter()
    pieces: List[str] = []
    async for token in llm.astream_answer(
        state["standalone_question"],
        blocks,
        state.get("history", []),
        state.get("verify_feedback", ""),
    ):
        pieces.append(token)
        await _emit(state, "token", {"content": token})
    answer = "".join(pieces).strip()
    ms = int((time.perf_counter() - t0) * 1000)
    _span(state, "generate", ms)

    # 从回答中提取实际使用的引用编号 → 构建引用列表
    used = sorted({int(m) for m in _CITE.findall(answer)})
    by_index = {b["index"]: b for b in blocks}
    citations = [
        {
            "index": i,
            "chunk_id": by_index[i]["chunk_id"],
            "doc_id": by_index[i]["doc_id"],
            "doc_name": by_index[i]["doc_name"],
            "section_path": by_index[i]["section_path"],
            "page": by_index[i]["page"],
            "score": by_index[i]["score"],
            "snippet": by_index[i]["text"][:200],
        }
        for i in used
        if i in by_index
    ]
    logger.info("[generate] %d 字符 / %d 处引用 / %d ms", len(answer), len(citations), ms)
    return {
        "answer": answer,
        "citations": citations,
        "context_blocks": blocks,
        "metrics": {**state.get("metrics", {}), "generation_ms": ms},
    }


# ---------------------------------------------------------------------------
# 5. 一致性校验(防幻觉·第二道闸)
# ---------------------------------------------------------------------------
async def verify(state: GraphState) -> Dict[str, Any]:
    settings = get_settings()
    llm = get_llm_service()
    await _emit(state, "status", {"stage": "verify", "message": "正在校验回答与说明书一致性..."})
    t0 = time.perf_counter()
    result = await llm.verify_answer(state["answer"], state.get("context_blocks", []))
    ms = int((time.perf_counter() - t0) * 1000)
    _span(state, "verify", ms)
    metrics = {**state.get("metrics", {}), "verify_ms": ms}
    logger.info("[verify] supported=%s notes=%s (%d ms)", result.supported, result.notes, ms)

    if result.supported:
        return {"verified": True, "verify_notes": result.notes, "metrics": metrics}

    retries = state.get("retries", 0) + 1
    if retries <= settings.generation_max_retries:
        feedback = result.notes + ("；" + "；".join(result.unsupported[:3]) if result.unsupported else "")
        return {
            "verified": False,
            "verify_notes": result.notes,
            "verify_feedback": feedback,
            "retries": retries,
            "metrics": metrics,
        }
    # 重试耗尽 → 安全拒答(不向用户输出未通过校验的内容)
    return {
        "verified": False,
        "verify_notes": result.notes,
        "route": "refuse",
        "refusal_reason": "生成内容未通过事实一致性校验, 为避免误导已拦截",
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# 6. 安全拒答
# ---------------------------------------------------------------------------
async def refuse(state: GraphState) -> Dict[str, Any]:
    reason = state.get("refusal_reason", "未检索到相关资料")
    message = REFUSAL_TEMPLATE.format(reason=reason)
    if state.get("retries", 0) > 0:
        await _emit(state, "reset", {"reason": "回答未通过一致性校验，已替换为安全回复"})
    pieces: List[str] = []
    for sent in split_sentences(message):
        pieces.append(sent)
        await _emit(state, "token", {"content": sent})
    logger.info("[refuse] %s", reason)
    return {
        "answer": "".join(pieces),
        "citations": [],
        "verified": False,
        "refusal_reason": reason,
    }
