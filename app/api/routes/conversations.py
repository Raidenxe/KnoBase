"""会话历史管理接口(按租户隔离)"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.security import (
    admin_required,
    auth_enabled,
    current_tenant_id,
    get_current_user,
    read_required,
    require_tenant_resource,
    write_required,
)
from app.services.history import get_conversation_store

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


def _owner_filter(user: dict) -> str:
    """认证开启且为真实登录用户时, 按其归属(user_id)隔离会话; 演示模式返回空=不隔离。"""
    if auth_enabled() and not user.get("is_anonymous"):
        return user.get("id", "")
    return ""


def _require_owner(store, conversation_id: str, user: dict) -> dict:
    """取会话并校验归属(tenant + owner); 否则 404, 避免泄露他人会话是否存在。"""
    conv = store.get_conversation(conversation_id, current_tenant_id(user), _owner_filter(user))
    if not conv:
        raise HTTPException(404, "会话不存在")
    require_tenant_resource(conv.get("tenant_id", ""), user)
    return conv


class RenameRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=60)


@router.get("", summary="会话列表")
def list_conversations(limit: int = 50, user: dict = Depends(get_current_user)) -> dict:
    return {"conversations": get_conversation_store().list_conversations(
        limit, current_tenant_id(user), _owner_filter(user))}


@router.get("/{conversation_id}", summary="会话详情(含全部消息)")
def get_conversation(conversation_id: str, user: dict = Depends(get_current_user)) -> dict:
    store = get_conversation_store()
    conv = _require_owner(store, conversation_id, user)
    conv["messages"] = store.get_messages(conversation_id)
    return conv


@router.get("/{conversation_id}/export", summary="导出会话(Markdown/Text)")
def export_conversation(
    conversation_id: str,
    format: str = "markdown",
    user: dict = Depends(read_required),
):
    from urllib.parse import quote

    from fastapi.responses import PlainTextResponse

    store = get_conversation_store()
    conv = _require_owner(store, conversation_id, user)
    msgs = store.get_messages(conversation_id)
    title = conv.get("title", conversation_id)

    if format == "text":
        body = [f"{title}", "=" * len(title), ""]
        for m in msgs:
            who = "我" if m["role"] == "user" else "助手"
            body.append(f"【{who}】\n{m['content']}\n")
        content = "\n".join(body)
    else:
        body = [f"# {title}", ""]
        for m in msgs:
            who = "**我**" if m["role"] == "user" else "**助手**"
            body.append(f"> {who}\n\n{m['content']}\n")
        content = "\n".join(body)
    media = "text/markdown" if format != "text" else "text/plain"
    fname = f"{title[:40]}.{'md' if format != 'text' else 'txt'}"
    # 中文/特殊字符文件名需按 RFC 5987 编码, 否则 Header 无法编码为 latin-1
    ascii_name = "conversation." + fname.rsplit(".", 1)[-1]
    disposition = (
        f"attachment; filename=\"{ascii_name}\""
        f"; filename*=UTF-8''{quote(fname)}"
    )
    return PlainTextResponse(
        content, media_type=media,
        headers={"Content-Disposition": disposition},
    )


class ShareRequest(BaseModel):
    ttl_seconds: Optional[int] = Field(
        None, ge=60, le=7 * 24 * 3600, description="链接有效期(秒), 缺省不过期"
    )


@router.post("/{conversation_id}/export/share", summary="生成会话只读分享链接")
def share_conversation(
    conversation_id: str, req: ShareRequest,
    user: dict = Depends(write_required),
) -> dict:
    from app.services.shares import get_share_store

    store = get_conversation_store()
    conv = _require_owner(store, conversation_id, user)
    msgs = store.get_messages(conversation_id)
    if not msgs:
        raise HTTPException(400, "会话为空, 无可分享内容")
    share = get_share_store().create(
        conversation_id, conv.get("tenant_id", ""), conv.get("title"),
        msgs, user.get("username", ""), ttl_seconds=req.ttl_seconds,
    )
    return share


@router.delete("/share/{token}", summary="撤销分享链接")
def revoke_share(token: str, user: dict = Depends(write_required)) -> dict:
    from app.services.shares import get_share_store

    get_share_store().delete(token)
    return {"revoked": token}


@router.delete("/{conversation_id}", summary="删除会话", dependencies=[Depends(write_required)])
def delete_conversation(conversation_id: str, user: dict = Depends(write_required)) -> dict:
    if not get_conversation_store().delete_conversation(
        conversation_id, current_tenant_id(user), _owner_filter(user)
    ):
        raise HTTPException(404, "会话不存在")
    from app.services.shares import get_share_store

    get_share_store().delete_by_conversation(conversation_id)
    return {"deleted": conversation_id}


@router.patch("/{conversation_id}", summary="重命名会话", dependencies=[Depends(write_required)])
def rename_conversation(conversation_id: str, req: RenameRequest, user: dict = Depends(write_required)) -> dict:
    store = get_conversation_store()
    _require_owner(store, conversation_id, user)
    if not store.rename_conversation(
        conversation_id, req.title, current_tenant_id(user), _owner_filter(user)
    ):
        raise HTTPException(400, "标题不合法")
    return {"id": conversation_id, "title": req.title.strip()[:60]}


@router.get("/admin/stats", summary="对话监控聚合(admin): 全量对话/拒答率/反馈/链路")
def admin_stats(limit: int = 200, _user: dict = Depends(admin_required)) -> dict:
    """后台对话监控: 汇总指定租户范围内的全量对话、拒答率、反馈与链路耗时。"""
    from app.services.feedback import get_feedback_store
    from app.services.trace_store import get_trace_store

    store = get_conversation_store()
    tenant_id = current_tenant_id(_user)
    convo_list = store.list_conversations(limit, tenant_id)

    total = 0
    refusal = 0
    conv_rows = []
    for c in convo_list:
        msgs = store.get_messages(c["id"])
        user_msgs = [m for m in msgs if m["role"] == "user"]
        assist_msgs = [m for m in msgs if m["role"] == "assistant"]
        total += len(user_msgs)
        refusal += sum(1 for m in assist_msgs if m["content"].startswith("抱歉，根据现有"))
        conv_rows.append({
            "id": c["id"], "title": c["title"],
            "created_at": c.get("created_at"), "updated_at": c.get("updated_at"),
            "messages": len(msgs), "users": len(user_msgs),
        })

    fb = get_feedback_store().stats(tenant_id)
    trace = get_trace_store().summary()

    return {
        "conversations": conv_rows,
        "stats": {
            "total_conversations": len(conv_rows),
            "total_questions": total,
            "refusal_count": refusal,
            "refusal_rate": round(refusal / total, 4) if total else 0.0,
            "feedback": fb,
        },
        "traces": trace,
    }