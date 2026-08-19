"""问答接口: 同步 JSON 与 SSE 流式两种模式"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.security import current_tenant_id, get_current_user
from app.services.chat_service import get_chat_service

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    conversation_id: Optional[str] = Field(
        None, description="会话 ID(缺省则新建会话, 多轮对话时传入)"
    )
    doc_version: Optional[int] = Field(
        None, description="按软件版本过滤检索(如 2 / 3 / 4; 缺省检索全部版本)"
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _resolve_conv_ownership(req: ChatRequest, user: dict) -> None:
    """多租户下校验会话归属(admin 可跨租户, 其余须同租户)。"""
    from app.core.security import require_tenant_resource
    from app.services.history import get_conversation_store

    if req.conversation_id:
        conv = get_conversation_store().get_conversation(
            req.conversation_id, current_tenant_id(user)
        )
        if not conv:
            raise HTTPException(404, f"会话不存在: {req.conversation_id}")
        require_tenant_resource(conv.get("tenant_id", ""), user)


@router.post("/chat", summary="提问(非流式)")
async def chat(req: ChatRequest, user: dict = Depends(get_current_user)) -> dict:
    _resolve_conv_ownership(req, user)
    result = await get_chat_service().chat(
        req.question, req.conversation_id, current_tenant_id(user), req.doc_version, user
    )
    return {
        "conversation_id": result.conversation_id,
        "answer": result.answer,
        "citations": result.citations,
        "verified": result.verified,
        "metrics": result.metrics,
        "refusal_reason": result.refusal_reason,
        "trace_id": result.trace_id,
        "message_id": result.message_id,
        "suggestions": result.suggestions,
    }


@router.post("/chat/stream", summary="提问(SSE 流式返回)")
async def chat_stream(req: ChatRequest, user: dict = Depends(get_current_user)) -> StreamingResponse:
    _resolve_conv_ownership(req, user)
    service = get_chat_service()
    tenant_id = current_tenant_id(user)

    async def event_gen():
        try:
            async for event in service.chat_stream(req.question, req.conversation_id, tenant_id, req.doc_version, user):
                yield _sse(event["event"], event["data"])
        except Exception as exc:  # noqa: BLE001
            yield _sse("error", {"detail": str(exc)})

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )