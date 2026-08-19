"""通知中心 API(用户端 + 管理端公告发布)。

用户端: 最近通知(下拉) / 未读数 / 分页列表(类型筛选) / 标记已读 / 全部已读 / 删除
管理端(owner/admin): 发布系统维护公告(广播全体或指定组) / 知识库更新公告
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import current_tenant_id, get_current_user, owner_required
from app.services.notify_store import (
    T_ANNOUNCE,
    T_KB_UPDATE,
    get_notify_store,
    tenant_user_ids,
)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


class BroadcastIn(BaseModel):
    ntype: str = "announce"
    title: str
    content: str = ""
    link: str = ""
    group_ids: List[str] = []          # 空 = 全体


class ReadBatchIn(BaseModel):
    ids: List[str]


TYPE_MAP = {
    "announce": T_ANNOUNCE,
    "kb_update": T_KB_UPDATE,
    "ticket_status": "ticket_status",
    "ticket_reply": "ticket_reply",
    "permission": "permission",
}
VALID_TYPES = {"announce", "kb_update", "ticket_status", "ticket_reply", "permission"}


# ---------------------------------------------------------------- 用户端
@router.get("/recent", summary="下拉面板最近 N 条通知")
def recent(limit: int = 5, user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    store = get_notify_store()
    if limit < 1 or limit > 20:
        raise HTTPException(400, "limit 取值 1~20")
    if not user.get("id"):
        return {"notifications": [], "unread": 0}
    return {
        "notifications": store.recent(tenant, user["id"], n=limit),
        "unread": store.unread_count(tenant, user["id"]),
    }


@router.get("/unread-count", summary="未读通知数")
def unread_count(user: dict = Depends(get_current_user)) -> dict:
    tenant = current_tenant_id(user)
    return {"unread": get_notify_store().unread_count(tenant, user.get("id", "")) if user.get(
        "id") else 0}


@router.get("", summary="通知分页列表(可筛选类型)")
def notification_list(
    ntype: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    user: dict = Depends(get_current_user),
) -> dict:
    tenant = current_tenant_id(user)
    if ntype and ntype not in VALID_TYPES:
        raise HTTPException(400, f"非法通知类型: {ntype}")
    if not user.get("id"):
        return {"notifications": [], "total": 0, "unread": 0}
    store = get_notify_store()
    items = store.list_notifications(tenant, user["id"], limit=limit, offset=offset, ntype=ntype)
    u = store.unread_count(tenant, user["id"])
    # 无类型筛选时统计总数
    if ntype:
        total = len(store.list_notifications(tenant, user["id"], limit=1000, offset=0, ntype=ntype))
    else:
        total = len(store.list_notifications(tenant, user["id"], limit=1000, offset=0))
    return {"notifications": items, "total": total, "unread": u}


@router.post("/{notify_id}/read", summary="标记单条已读")
def mark_read(notify_id: str, user: dict = Depends(get_current_user)) -> dict:
    ok = get_notify_store().mark_read(notify_id, user.get("id", ""))
    if not ok:
        raise HTTPException(404, "通知不存在")
    return {"read": True}


@router.post("/read-all", summary="全部已读")
def mark_all_read(user: dict = Depends(get_current_user)) -> dict:
    n = get_notify_store().mark_all_read(current_tenant_id(user), user.get("id", ""))
    return {"read": n}


@router.delete("/{notify_id}", summary="删除单条通知")
def delete_one(notify_id: str, user: dict = Depends(get_current_user)) -> dict:
    ok = get_notify_store().delete(notify_id, user.get("id", ""))
    if not ok:
        raise HTTPException(404, "通知不存在")
    return {"deleted": True}


@router.post("/delete-batch", summary="批量删除通知")
def delete_batch(body: ReadBatchIn, user: dict = Depends(get_current_user)) -> dict:
    n = get_notify_store().delete_many(body.ids or [], user.get("id", ""))
    return {"deleted": n}


# ---------------------------------------------------------------- 管理端 公告/知识库更新广播
@router.post("/broadcast", summary="发布公告/知识库更新(owner/admin)", dependencies=[Depends(owner_required)])
def broadcast(body: BroadcastIn, user: dict = Depends(get_current_user)) -> dict:
    if not body.title.strip():
        raise HTTPException(400, "通知标题不能为空")
    if body.ntype not in (T_ANNOUNCE, T_KB_UPDATE):
        raise HTTPException(400, "仅支持公告(announce)或知识库更新(kb_update)")
    tenant = current_tenant_id(user)
    if body.ntype == T_ANNOUNCE and not body.content.strip():
        raise HTTPException(400, "系统维护公告需填写内容")
    store = get_notify_store()
    uids = tenant_user_ids(tenant, group_ids=body.group_ids or None)
    if not uids:
        return {"sent": 0, "hint": "认证关闭或暂无目标用户, 未落库"}
    n = store.make_broadcast(tenant, uids, body.ntype, body.title.strip(),
                             body.content, body.link)
    return {"sent": n}