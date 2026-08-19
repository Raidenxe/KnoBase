"""工单系统 API(用户端 + 管理端)。

用户端: 提交工单 / 我的工单 / 详情 / 回复 / 催办 / 确认关闭 / 重新打开
管理端(admin/owner): 工单总览 / 详情 / 回复 / 状态流转 / 统计仪表盘

自动通知: 状态变更 / 管理员回复时, 向工单提交人生成通知中心消息。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import get_settings
from app.core.security import (
    current_tenant_id,
    get_current_user,
    owner_required,
)
from app.services.notify_store import (
    T_TICKET_REPLY,
    T_TICKET_STATUS,
    get_notify_store,
)
from app.services.ticket_store import (
    STATUS_CN,
    TICKET_TYPES,
    get_ticket_store,
)

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])

TICKET_URL = lambda tid: f"/tickets.html?tk={tid}"  # noqa: E731


class ReplyIn(BaseModel):
    content: str


class StatusIn(BaseModel):
    status: str
    note: str = ""


TICKET_STATUSES = list(STATUS_CN.keys())


def _footer() -> str:
    """自动通知附带的小字提示。"""
    return "如有疑问可进入工单详情查看。"


# ---------------------------------------------------------------- 用户端
@router.post("", summary="提交工单(含可选附件)")
async def create_ticket(
    ticket_type: str = Form(...),
    title: str = Form(""),
    description: str = Form(""),
    urgency: str = Form("normal"),
    attachment: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_user),
) -> dict:
    tenant = current_tenant_id(user)
    store = get_ticket_store()
    try:
        saved = _save_attachment(tenant, attachment) if attachment else ""
        t = store.create(tenant, user.get("id", ""), user.get("username", "") or "匿名",
                         ticket_type, title, description, urgency, saved)
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "id": t["id"], "status": t["status"],
        "message": f"已提交，管理员将在 1-2 个工作日内处理",
        "link": TICKET_URL(t["id"]),
    }


def _save_attachment(tenant: str, uf: UploadFile) -> str:
    settings = get_settings()
    max_bytes = 10 * 1024 * 1024
    data = uf.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("附件大小不能超过 10MB")
    name = (uf.filename or "file").replace("/", "_").replace("\\", "_")
    d = Path(settings.uploads_dir) / "tickets" / tenant
    d.mkdir(parents=True, exist_ok=True)
    import uuid

    fname = f"{uuid.uuid4().hex[:12]}_{name}"
    (d / fname).write_bytes(data)
    return f"{d.name}/{fname}"


def _attach_url(t: dict) -> str:
    if not t.get("attachment"):
        return ""
    settings = get_settings()
    return f"/uploads/tickets/{t['tenant_id']}/{Path(t['attachment']).name}"


@router.get("", summary="我的工单列表(分页/筛选)")
def my_tickets(
    status: Optional[str] = None,
    ticket_type: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_current_user),
) -> dict:
    tenant = current_tenant_id(user)
    if status and status not in TICKET_STATUSES:
        raise HTTPException(400, f"非法状态: {status}")
    store = get_ticket_store()
    items = store.list(tenant, creator_id=user.get("id", ""),
                       status=status, ticket_type=ticket_type, limit=limit, offset=offset)
    return {
        "tickets": items,
        "total": store.count(tenant, creator_id=user.get("id", ""), status=status),
    }


# ---------------------------------------------------------------- 管理端
# 注意: /admin 及 /admin/* 静态路径必须先于 /{ticket_id} 注册, 否则被动态路由遮蔽
@router.get("/admin", summary="工单总览(owner/admin)", dependencies=[Depends(owner_required)])
def admin_list(
    status: Optional[str] = None,
    ticket_type: Optional[str] = None,
    urgent: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_current_user),
) -> dict:
    tenant = current_tenant_id(user)
    if status and status not in TICKET_STATUSES:
        raise HTTPException(400, f"非法状态: {status}")
    if urgent not in (None, "urgent"):
        raise HTTPException(400, f"非法紧急筛选: {urgent}")
    store = get_ticket_store()
    items = store.list(tenant, status=status, ticket_type=ticket_type,
                       limit=limit, offset=offset, sort_urgent_first=(urgent == "urgent"))
    for t in items:
        _strip(t)
    return {
        "tickets": items,
        "total": store.count(tenant, status=status),
        "reminded": [t["id"] for t in items if t["remind_count"] > 0],
        "type_options": TICKET_TYPES,
        "status_options": STATS_OPTIONS(),
    }


def STATS_OPTIONS():
    return [{"value": k, "label": v} for k, v in STATUS_CN.items()]


@router.get("/admin/stats", summary="工单统计仪表盘(owner/admin)", dependencies=[Depends(owner_required)])
def admin_stats(user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    store = get_ticket_store()
    s = store.stats(tenant)
    s["trend"] = store.recent_ops(tenant)
    # 常用标签: 各状态中文
    s["by_status_cn"] = {STATUS_CN.get(k, k): v for k, v in s["by_status"].items()}
    return s


@router.get("/admin/{ticket_id}", summary="工单详情(owner/admin)")
def admin_detail(ticket_id: str, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    t = get_ticket_store().get(ticket_id, tenant)
    if not t:
        raise HTTPException(404, "工单不存在")
    _strip(t)
    t["attachment_url"] = _attach_url(t)
    return t


@router.post("/admin/{ticket_id}/reply", summary="管理员回复(owner/admin)")
def admin_reply(ticket_id: str, body: ReplyIn, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    try:
        t = get_ticket_store().add_reply(
            ticket_id, tenant, user.get("id", ""), user.get("username", "") or "管理员",
            user.get("role", ""), body.content.strip(), is_admin=True)
    except (LookupError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _notify_ticket_reply(t, body.content.strip(), is_owner=False)
    _strip(t)
    t["attachment_url"] = _attach_url(t)
    return t


@router.post("/admin/{ticket_id}/status", summary="状态流转(owner/admin)")
def admin_transition(ticket_id: str, body: StatusIn, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    try:
        t = get_ticket_store().transition(
            ticket_id, tenant, body.status, body.note,
            by_id=user.get("id", ""), by_name=user.get("username", "") or "管理员")
    except (LookupError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _notify_ticket_status(t, body.note)
    _strip(t)
    t["attachment_url"] = _attach_url(t)
    return t


@router.get("/{ticket_id}", summary="我的工单详情")
def my_ticket_detail(ticket_id: str, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    t = get_ticket_store().get(ticket_id, tenant)
    if not t or t["creator_id"] != user.get("id", ""):
        raise HTTPException(404, "工单不存在")
    _strip(t)
    t["attachment_url"] = _attach_url(t)
    return t


@router.post("/{ticket_id}/replies", summary="回复自己的工单")
def add_user_reply(ticket_id: str, body: ReplyIn, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    try:
        t = get_ticket_store().add_reply(
            ticket_id, tenant, user.get("id", ""), user.get("username", "") or "匿名",
            user.get("role", ""), body.content.strip(),
            is_admin=user.get("role") in ("admin", "owner"),
        )
    except (LookupError, PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    # 管理员回复时向提交人推送(若本人回复则无需通知)
    if user.get("role") in ("admin", "owner"):
        _notify_ticket_reply(t, body.content.strip(), is_owner=True)
    _strip(t)
    t["attachment_url"] = _attach_url(t)
    return t


@router.post("/{ticket_id}/remind", summary="催办工单")
def remind_ticket(ticket_id: str, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    try:
        r = get_ticket_store().remind(ticket_id, tenant, user.get("id", ""))
    except (LookupError, PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return r


@router.post("/{ticket_id}/close", summary="确认关闭(提交人)")
def close_ticket(ticket_id: str, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    try:
        t = get_ticket_store().close(ticket_id, tenant, user.get("id", ""))
    except (LookupError, PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _strip(t)
    t["attachment_url"] = _attach_url(t)
    return t


@router.post("/{ticket_id}/reopen", summary="重新打开(提交人)")
def reopen_ticket(ticket_id: str, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    try:
        t = get_ticket_store().reopen(ticket_id, tenant, user.get("id", ""))
    except (LookupError, PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _strip(t)
    t["attachment_url"] = _attach_url(t)
    return t





# ---------------------------------------------------------------- 辅助
def _strip(t: dict) -> None:
    """占位: 列表项本身不含 replies(见 ticket_store.list 用 _row), 详情需保留回复时间线, 故不再移除。
    仅用于统一接口出口钩子, 便于未来裁剪字段。"""
    return


def _notify_ticket_status(t: dict, note: str = "") -> None:
    if not t["creator_id"]:
        return
    status_cn = STATUS_CN.get(t["status"], t["status"])
    title = f"工单 {t['id']} 状态变更"
    content = f"您提交的工单 {t['id']} 已变更为【{status_cn}】。{note}"
    get_notify_store().notify_user(t["tenant_id"], t["creator_id"],
                                   T_TICKET_STATUS, title, content, TICKET_URL(t["id"]))


def _notify_ticket_reply(t: dict, content: str, is_owner: bool = False) -> None:
    if not t["creator_id"] or is_owner:
        return
    title = f"管理员回复了您的工单 {t['id']}"
    get_notify_store().notify_user(t["tenant_id"], t["creator_id"],
                                   T_TICKET_REPLY, title,
                                   content[:200], TICKET_URL(t["id"]))