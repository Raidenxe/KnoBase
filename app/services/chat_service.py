"""聊天编排服务: 组装历史上下文 → 运行 LangGraph 对话图 → 持久化会话。

流式模式通过 asyncio.Queue 将图节点内的 SSE 事件实时推送出来,
图执行完毕后发送 done 事件(含最终权威回答/引用/指标)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from app.config import get_settings
from app.graph.builder import build_graph
from app.services.history import get_conversation_store
from app.services.trace_store import get_trace_store

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    conversation_id: str
    answer: str
    citations: List[Dict[str, Any]] = field(default_factory=list)
    verified: bool = False
    metrics: Dict[str, Any] = field(default_factory=dict)
    refusal_reason: str = ""
    trace_id: str = ""
    message_id: int = 0
    suggestions: List[str] = field(default_factory=list)  # 追问/推荐问题


class ChatService:
    def __init__(self) -> None:
        self.graph = build_graph()

    def _prepare(self, question: str, conversation_id: Optional[str], tenant_id: str):
        store = get_conversation_store()
        settings = get_settings()
        if conversation_id:
            if not store.get_conversation(conversation_id, tenant_id):
                raise ValueError(f"会话不存在: {conversation_id}")
        else:
            conv = store.create_conversation(question.strip()[:30] or "新会话", tenant_id)
            conversation_id = conv["id"]
        history = store.history_window(
            conversation_id, settings.history_window_messages
        )
        store.append_message(conversation_id, "user", question)
        return conversation_id, history

    # ------------------------------------------------------------------
    # 非流式
    # ------------------------------------------------------------------
    async def chat(
        self, question: str, conversation_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        doc_version: Optional[int] = None,
        user: Optional[dict] = None,
    ) -> ChatResult:
        tenant_id = tenant_id or get_settings().default_tenant
        conversation_id, history = self._prepare(question, conversation_id, tenant_id)
        trace_id = get_trace_store().start("chat", tenant_id)
        t0 = time.perf_counter()
        try:
            final: Dict[str, Any] = await self.graph.ainvoke(
                {
                    "conversation_id": conversation_id,
                    "question": question,
                    "history": history,
                    "tenant_id": tenant_id,
                    "trace_id": trace_id,
                    "doc_version": doc_version,
                    "category_scope": self._category_scope(user),
                }
            )
        except Exception:  # noqa: BLE001
            get_trace_store().finish(trace_id, "error")
            raise
        total_ms = int((time.perf_counter() - t0) * 1000)
        get_trace_store().finish(trace_id)
        return await self._finalize(
            conversation_id, final, total_ms, trace_id, question, tenant_id
        )

    # ------------------------------------------------------------------
    # 流式(SSE 事件迭代器)
    # ------------------------------------------------------------------
    async def chat_stream(
        self, question: str, conversation_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        doc_version: Optional[int] = None,
        user: Optional[dict] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        tenant_id = tenant_id or get_settings().default_tenant
        conversation_id, history = self._prepare(question, conversation_id, tenant_id)
        trace_id = get_trace_store().start("chat", tenant_id)
        queue: asyncio.Queue = asyncio.Queue()
        final_result: Dict[str, Any] = {}
        error: Optional[str] = None

        async def emit(event: Dict[str, Any]) -> None:
            await queue.put(event)

        async def run_graph() -> None:
            nonlocal final_result, error
            try:
                final_result = await self.graph.ainvoke(
                    {
                        "conversation_id": conversation_id,
                        "question": question,
                        "history": history,
                        "tenant_id": tenant_id,
                        "trace_id": trace_id,
                        "emit": emit,
                        "doc_version": doc_version,
                        "category_scope": self._category_scope(user),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("对话图执行失败")
                error = str(exc)
            finally:
                await queue.put(None)  # 结束哨兵

        t0 = time.perf_counter()
        task = asyncio.create_task(run_graph())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
        await task
        total_ms = int((time.perf_counter() - t0) * 1000)

        if error:
            get_trace_store().finish(trace_id, "error")
            yield {"event": "error", "data": {"detail": error or "内部错误"}}
            return

        get_trace_store().finish(trace_id)
        result = await self._finalize(
            conversation_id, final_result, total_ms, trace_id, question, tenant_id
        )
        yield {
            "event": "done",
            "data": {
                "conversation_id": result.conversation_id,
                "answer": result.answer,
                "citations": result.citations,
                "verified": result.verified,
                "metrics": result.metrics,
                "refusal_reason": result.refusal_reason,
                "trace_id": result.trace_id,
                "message_id": result.message_id,
                "suggestions": result.suggestions,
            },
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _category_scope(user: Optional[dict]):
        """把用户可读分类注入检索链路; 未提供用户或未授权(demo)时返回 None(不过滤)。"""
        if not user:
            return None
        from app.core.access import readable_categories

        return readable_categories(user)

    # ------------------------------------------------------------------
    async def _finalize(
        self, conversation_id: str, final: Dict[str, Any], total_ms: int,
        trace_id: str = "", question: str = "", tenant_id: str = "",
    ) -> ChatResult:
        store = get_conversation_store()
        answer = final.get("answer", "")
        citations = final.get("citations", [])
        msg = store.append_message(conversation_id, "assistant", answer, citations)
        metrics = {**(final.get("metrics") or {}), "total_ms": total_ms}
        logger.info(
            "[chat] conv=%s 检索=%dms 生成=%dms 校验=%dms 总计=%dms 引用=%d",
            conversation_id,
            metrics.get("retrieval_ms", -1),
            metrics.get("generation_ms", -1),
            metrics.get("verify_ms", -1),
            total_ms,
            len(citations),
        )
        # 命中统计 + 追问建议(不影响主链路; 失败静默降级)
        suggestions: List[str] = []
        try:
            self._record_hits(citations, tenant_id)
            suggestions = await self._gen_suggestions(question, citations)
        except Exception:  # noqa: BLE001
            logger.warning("追问/命中统计生成失败(已忽略)", exc_info=True)
        return ChatResult(
            conversation_id=conversation_id,
            answer=answer,
            citations=citations,
            verified=bool(final.get("verified")),
            metrics=metrics,
            refusal_reason=final.get("refusal_reason", ""),
            trace_id=trace_id,
            message_id=msg["id"],
            suggestions=suggestions,
        )

    # ------------------------------------------------------------------
    def _record_hits(self, citations: List[Dict[str, Any]], tenant_id: str) -> None:
        from app.services.doc_stats import get_doc_stats_store

        doc_ids = [c.get("doc_id", "") for c in (citations or [])]
        doc_names = {c.get("doc_id", ""): c.get("doc_name", "") for c in (citations or [])}
        get_doc_stats_store().record_hits(doc_ids, doc_names, tenant_id)

    async def _gen_suggestions(
        self, question: str, citations: List[Dict[str, Any]]
    ) -> List[str]:
        if not citations:
            return []
        from app.core.llm import get_llm_service

        return await get_llm_service().generate_followups(question, citations)


_chat_service: Optional[ChatService] = None


def get_chat_service() -> ChatService:
    global _chat_service
    if _chat_service is None:
        _chat_service = ChatService()
    return _chat_service
