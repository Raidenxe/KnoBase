"""用户反馈接口: 提交点赞/点踩(可带文字), 管理员查询统计。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import (
    current_tenant_id,
    get_current_user,
    require_tenant_resource,
    write_required,
)
from app.services.feedback import get_feedback_store
from app.services.history import get_conversation_store

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_id: int
    rating: str = Field(..., pattern="^(up|down)$", description="up / down")
    comment: str = ""  # 可选文字反馈


@router.post("", summary="提交/更新一条回答反馈", dependencies=[Depends(write_required)])
def submit_feedback(req: FeedbackRequest, user: dict = Depends(write_required)) -> dict:
    """记录该回答的点赞/点踩, 同一消息再次提交即覆盖。"""
    store = get_conversation_store()
    conv = store.get_conversation(req.conversation_id, current_tenant_id(user))
    if not conv:
        raise HTTPException(404, "会话不存在")
    require_tenant_resource(conv.get("tenant_id", ""), user)
    msgs = store.get_messages(req.conversation_id)
    if not any(m["id"] == req.message_id for m in msgs):
        raise HTTPException(404, f"消息不存在: {req.message_id}")
    return get_feedback_store().submit(
        req.conversation_id, req.message_id, req.rating, req.comment,
        current_tenant_id(user), user.get("username", ""),
    )


@router.get("/stats", summary="反馈统计(点赞/点踩比例)", dependencies=[Depends(write_required)])
def feedback_stats(user: dict = Depends(write_required)) -> dict:
    return get_feedback_store().stats(current_tenant_id(user))