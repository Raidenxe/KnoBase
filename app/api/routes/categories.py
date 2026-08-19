"""知识库分类管理 + 组×分类 授权矩阵接口。

分类是被授权资源(What), 用户组是权限载体(Group), 授权矩阵把二者绑定为
"谁(组)能读/能管哪些分类"。检索问答链路(nodes.retrieve)据此过滤命中片段。

注意: 静态路径 (/grants*) 必须注册在动态路径 (/{category}) 之前, 否则被遮蔽。
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.core.security import current_tenant_id, get_current_user, owner_required, read_required
from app.services.auth_store import get_auth_store
from app.services.audit import audit
from app.services.doc_meta import get_doc_meta_store

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


def _notify_permission(tenant_id: str, group_id: str, category: str, granted: bool) -> None:
    """权限变更提醒 → 通知中心(通知该组被授予/收回分类读取权限的成员)。"""
    if not tenant_id:
        return
    from app.services.notify_store import T_PERMISSION, get_notify_store

    member_ids = get_auth_store().group_members(group_id)
    if not member_ids:
        return
    title = f"知识库访问权限更新"
    content = (f"您的知识库访问权限已更新：{'新增' if granted else '收回'}分类【{category}】"
               f"的{'读取' if granted else ''}权限。")
    store = get_notify_store()
    for u in member_ids:
        store.notify_user(tenant_id, u["id"], T_PERMISSION, title, content,
                          link="/documents-browser")


class CreateCategoryRequest(BaseModel):
    name: str
    description: str = ""


class UpdateCategoryRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SetGrantRequest(BaseModel):
    group_id: str
    category: str
    can_read: bool = False
    can_manage: bool = False


class BatchGrantRequest(BaseModel):
    category: str
    grants: List[SetGrantRequest] = []


# ------------------------------------------------------------------
# 组×分类 授权矩阵(P0 分类授权) — 必须先于 /{category} 注册
# ------------------------------------------------------------------
@router.get("/grants/matrix", summary="授权矩阵视图(组 × 分类)", dependencies=[Depends(read_required)])
def grants_matrix(user: dict = Depends(get_current_user)) -> dict:
    tenant_id = current_tenant_id(user)
    meta = get_doc_meta_store()
    groups = get_auth_store().list_groups()
    categories = meta.list_categories(tenant_id)
    cells: dict = {}
    for g in meta.get_category_grants(tenant_id):
        cells.setdefault(g["group_id"], {})[g["category"]] = {
            "can_read": g["can_read"], "can_manage": g["can_manage"],
        }
    return {
        "groups": [{"id": g["id"], "name": g["name"]} for g in groups],
        "categories": [c["name"] for c in categories],
        "cells": cells,
    }


@router.put("/grants", summary="设置某组对某分类的授权(owner/admin)", dependencies=[Depends(owner_required)])
def set_grant(req: SetGrantRequest, user: dict = Depends(get_current_user)) -> dict:
    try:
        res = get_doc_meta_store().set_category_grant(
            req.group_id, req.category, current_tenant_id(user),
            can_read=req.can_read, can_manage=req.can_manage,
            by=user.get("username", ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit("grant", actor=user.get("username", ""), tenant_id=current_tenant_id(user),
          role=user.get("role", ""),
          target=f"grant:{req.group_id}:{req.category}",
          detail=f"read={req.can_read} manage={req.can_manage}")
    _notify_permission(current_tenant_id(user), req.group_id, req.category,
                       bool(req.can_read))
    return res


@router.put("/grants/batch", summary="批量设置某分类多点授权(owner/admin)", dependencies=[Depends(owner_required)])
def batch_set_grants(req: BatchGrantRequest, user: dict = Depends(get_current_user)) -> dict:
    tenant_id = current_tenant_id(user)
    meta = get_doc_meta_store()
    try:
        for g in req.grants:
            meta.set_category_grant(
                g.group_id, g.category, tenant_id,
                can_read=g.can_read, can_manage=g.can_manage,
                by=user.get("username", ""),
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit("grant_batch", actor=user.get("username", ""), tenant_id=tenant_id,
          role=user.get("role", ""), target=req.category,
          detail=f"{len(req.grants)} 组授权已更新")
    for g in req.grants:
        _notify_permission(tenant_id, g.group_id, g.category, bool(g.can_read))
    return {"updated": len(req.grants)}


# ------------------------------------------------------------------
# 分类 CRUD(P0 分类管理)
# ------------------------------------------------------------------
@router.get("", summary="知识库分类列表(显式定义)", dependencies=[Depends(read_required)])
def list_categories(user: dict = Depends(get_current_user)) -> dict:
    return {"categories": get_doc_meta_store().list_categories(current_tenant_id(user))}


@router.post("", summary="新增知识库分类(owner/admin)", dependencies=[Depends(owner_required)])
def create_category(req: CreateCategoryRequest, user: dict = Depends(get_current_user)) -> dict:
    try:
        cat = get_doc_meta_store().create_category(
            req.name, current_tenant_id(user), req.description, by=user.get("username", "")
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return cat


@router.put("/{category}", summary="编辑分类(改名/描述, owner/admin)", dependencies=[Depends(owner_required)])
def update_category(category: str, req: UpdateCategoryRequest, user: dict = Depends(get_current_user)) -> dict:
    meta = get_doc_meta_store()
    tenant_id = current_tenant_id(user)
    try:
        if req.name and req.name != category:
            meta.rename_category(category, req.name, tenant_id)
            category = req.name
        cat = meta.update_category(category, tenant_id, req.description)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return cat


@router.delete("/{category}", summary="删除分类(分类下仍有文档时拒绝)", dependencies=[Depends(owner_required)])
def delete_category(category: str, user: dict = Depends(get_current_user)) -> dict:
    try:
        return get_doc_meta_store().delete_category(category, current_tenant_id(user))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc