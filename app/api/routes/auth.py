"""认证与用户/租户管理接口(仅 RAG_AUTH_MODE=on 时生效)。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.security import (
    admin_required,
    create_access_token,
    get_current_user,
    owner_required,
)
from app.services.auth_store import get_auth_store

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "member"
    tenant_id: str = ""
    display_name: str = ""
    email: str = ""
    group_ids: List[str] = []


class UpdateUserRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    group_ids: Optional[List[str]] = None


class ResetPasswordRequest(BaseModel):
    new_password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    email: Optional[str] = None


class CreateGroupRequest(BaseModel):
    name: str
    description: str = ""


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class GroupMembersRequest(BaseModel):
    user_ids: List[str]


@router.post("/login", summary="登录获取 JWT")
def login(req: LoginRequest, request: Request) -> dict:
    store = get_auth_store()
    # 账号锁定检查(防暴力枚举)
    status = store.login_status(req.username)
    if status["locked"]:
        raise HTTPException(401, f"账号已临时锁定, 请 {status['retry_in']} 秒后再试")
    user = store.authenticate(req.username, req.password)
    if not user:
        store.register_login_failure(req.username)
        raise HTTPException(401, "用户名或密码错误")
    store.register_login_success(req.username)
    token = create_access_token(user)
    # 记录登录日志: IP + 设备(User-Agent) + 时间, 供用户自查/安全审计
    ip = (request.client.host if request.client else "") or ""
    if xff := request.headers.get("x-forwarded-for"):
        ip = xff.split(",")[0].strip() or ip
    store.record_login(
        user["id"], ip=ip,
        device=(request.headers.get("user-agent", "") or "")[:200],
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"], "username": user["username"],
            "role": user["role"], "tenant_id": user["tenant_id"],
            "must_change_password": bool(user.get("must_change_password")),
        },
    }


@router.get("/me", summary="当前用户信息")
def me(user: dict = Depends(get_current_user)) -> dict:
    return {
        "id": user["id"], "username": user["username"],
        "role": user["role"], "tenant_id": user["tenant_id"],
        "is_anonymous": bool(user.get("is_anonymous")),
        "must_change_password": bool(user.get("must_change_password")),
    }


@router.get("/tenants", summary="租户列表(admin)", dependencies=[Depends(admin_required)])
def list_tenants() -> dict:
    return {"tenants": get_auth_store().list_tenants()}


@router.post("/users", summary="创建用户(owner/admin)", dependencies=[Depends(owner_required)])
def create_user(req: CreateUserRequest, operator: dict = Depends(get_current_user)) -> dict:
    if req.role not in ("admin", "owner", "member", "viewer"):
        raise HTTPException(400, f"非法角色: {req.role}")
    if operator.get("role") != "admin" and req.tenant_id and req.tenant_id != operator["tenant_id"]:
        raise HTTPException(403, "无权在本租户外创建用户")
    store = get_auth_store()
    scope_tenant = req.tenant_id or operator["tenant_id"]
    try:
        user = store.create_user(
            req.username, req.password, scope_tenant, req.role, req.display_name, req.email
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # 关联用户组
    store.replace_user_groups(user["id"], req.group_ids or [])
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "tenant_id": user["tenant_id"]}


@router.get("/users", summary="用户列表(owner/admin), 支持模糊搜索并附用户组")
def list_users(
    tenant_id: Optional[str] = None,
    q: Optional[str] = None,
    owner: dict = Depends(owner_required),
) -> dict:
    scope = tenant_id if owner.get("role") == "admin" else owner["tenant_id"]
    store = get_auth_store()
    users = store.list_users(scope)
    if q:
        ql = q.lower()
        users = [
            u for u in users
            if ql in (u["username"] or "").lower() or ql in (u.get("email") or "").lower()
        ]
    enriched = store.users_with_group_names(users)
    # 敏感字段脱敏: 绝不向前端返回口令哈希
    for u in enriched:
        u.pop("password_hash", None)
    return {"users": enriched}


@router.get("/users/{user_id}", summary="用户详情(owner/admin)", dependencies=[Depends(owner_required)])
def get_user_detail(user_id: str, owner: dict = Depends(get_current_user)) -> dict:
    store = get_auth_store()
    user = store.get_user(user_id)
    if not user:
        raise HTTPException(404, "用户不存在")
    if owner.get("role") != "admin" and user["tenant_id"] != owner["tenant_id"]:
        raise HTTPException(403, "无权查看其他租户用户")
    groups = store.user_groups(user_id)
    user = dict(user)
    user.pop("password_hash", None)  # 脱敏: 不返回口令哈希
    user["groups"] = [{"id": g["id"], "name": g["name"]} for g in groups]
    return user


@router.put("/users/{user_id}", summary="编辑用户资料/角色/用户组(owner/admin)", dependencies=[Depends(owner_required)])
def update_user(user_id: str, req: UpdateUserRequest, owner: dict = Depends(get_current_user)) -> dict:
    store = get_auth_store()
    cur = store.get_user(user_id)
    if not cur:
        raise HTTPException(404, "用户不存在")
    if owner.get("role") != "admin" and cur["tenant_id"] != owner["tenant_id"]:
        raise HTTPException(403, "无权编辑其他租户用户")
    if req.display_name is not None or req.email is not None:
        try:
            store.update_profile(user_id, req.display_name, req.email)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if req.role is not None:
        if req.role not in ("admin", "owner", "member", "viewer"):
            raise HTTPException(400, f"非法角色: {req.role}")
        store.update_role(user_id, req.role)
    if req.group_ids is not None:
        store.replace_user_groups(user_id, req.group_ids)
    return {"id": user_id, "updated": True}


@router.post("/users/{user_id}/disable", summary="禁用用户(owner/admin)", dependencies=[Depends(owner_required)])
def disable_user(user_id: str, owner: dict = Depends(get_current_user)) -> dict:
    store = get_auth_store()
    cur = store.get_user(user_id)
    if not cur:
        raise HTTPException(404, "用户不存在")
    if cur["id"] == owner.get("id"):
        raise HTTPException(400, "不能禁用自己")
    store.set_active(user_id, False)
    return {"id": user_id, "is_active": False}


@router.post("/users/{user_id}/enable", summary="启用用户(owner/admin)", dependencies=[Depends(owner_required)])
def enable_user(user_id: str) -> dict:
    get_auth_store().set_active(user_id, True)
    return {"id": user_id, "is_active": True}


@router.post("/users/{user_id}/reset-password", summary="重置密码并强制下次修改(owner/admin)", dependencies=[Depends(owner_required)])
def reset_user_password(user_id: str, req: ResetPasswordRequest) -> dict:
    from app.services.auth_store import validate_password

    if not req.new_password:
        raise HTTPException(400, "新密码不能为空")
    reason = validate_password(req.new_password)
    if reason:
        raise HTTPException(400, reason)
    get_auth_store().reset_password(user_id, req.new_password)
    return {"id": user_id, "reset": True}


@router.delete("/users/{user_id}", summary="删除用户(owner/admin)")
def delete_user(user_id: str, owner: dict = Depends(get_current_user)) -> dict:
    store = get_auth_store()
    cur = store.get_user(user_id)
    if not cur:
        raise HTTPException(404, "用户不存在")
    if cur["id"] == owner.get("id"):
        raise HTTPException(400, "不能删除自己")
    store.delete_user(user_id)
    return {"id": user_id, "deleted": True}


# ------------------------------------------------------------------
# 个人信息(本人可调)
# ------------------------------------------------------------------
@router.get("/profile", summary="个人资料")
def my_profile(user: dict = Depends(get_current_user)) -> dict:
    store = get_auth_store()
    u = store.get_user(user["id"]) or {}
    return {
        "id": u.get("id"), "username": u.get("username"),
        "display_name": u.get("display_name", ""), "email": u.get("email", ""),
        "role": u.get("role"), "tenant_id": u.get("tenant_id"),
        "must_change_password": bool(u.get("must_change_password")),
        "last_login_at": u.get("last_login_at", 0),
        "created_at": u.get("created_at"),
        "groups": [{"id": g["id"], "name": g["name"]} for g in store.user_groups(user["id"])],
    }


@router.put("/profile", summary="修改个人资料(姓名/邮箱)")
def update_my_profile(req: UpdateProfileRequest, user: dict = Depends(get_current_user)) -> dict:
    get_auth_store().update_profile(user["id"], req.display_name, req.email)
    return {"updated": True}


@router.post("/profile/change-password", summary="修改密码(需验证旧密码)")
def change_my_password(req: ChangePasswordRequest, user: dict = Depends(get_current_user)) -> dict:
    from app.services.auth_store import validate_password

    if not req.new_password:
        raise HTTPException(400, "新密码不能为空")
    if req.old_password == req.new_password:
        raise HTTPException(400, "新密码不能与旧密码相同")
    reason = validate_password(req.new_password)
    if reason:
        raise HTTPException(400, reason)
    if not get_auth_store().change_password(user["id"], req.old_password, req.new_password):
        raise HTTPException(400, "旧密码不正确")
    return {"updated": True}


@router.get("/profile/login-logs", summary="我的登录日志(IP/时间/设备)")
def my_login_logs(limit: int = 20, user: dict = Depends(get_current_user)) -> dict:
    return {
        "logs": get_auth_store().list_login_logs(user["id"], max(1, min(limit, 100))),
        "total_hint": "最近登录记录",
    }


# ------------------------------------------------------------------
# 用户组管理
# ------------------------------------------------------------------
@router.get("/groups", summary="用户组列表(含成员数)", dependencies=[Depends(owner_required)])
def list_groups() -> dict:
    return {"groups": get_auth_store().list_groups()}


@router.post("/groups", summary="新增用户组(owner/admin)", dependencies=[Depends(owner_required)])
def create_group(req: CreateGroupRequest, user: dict = Depends(get_current_user)) -> dict:
    try:
        g = get_auth_store().create_group(req.name, req.description, by=user.get("username", ""))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return g


@router.put("/groups/{group_id}", summary="编辑用户组(owner/admin)", dependencies=[Depends(owner_required)])
def update_group(group_id: str, req: UpdateGroupRequest) -> dict:
    try:
        g = get_auth_store().update_group(group_id, req.name, req.description)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return g


@router.delete("/groups/{group_id}", summary="删除用户组(组内有成员时拒绝)", dependencies=[Depends(admin_required)])
def delete_group(group_id: str) -> dict:
    try:
        get_auth_store().delete_group(group_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"deleted": True}


@router.get("/groups/{group_id}/members", summary="组成员列表(owner/admin)", dependencies=[Depends(owner_required)])
def group_members(group_id: str) -> dict:
    members = get_auth_store().group_members(group_id)
    return {"members": members}


@router.put("/groups/{group_id}/members", summary="设置组成员(全量替换, owner/admin)", dependencies=[Depends(owner_required)])
def set_group_members(group_id: str, req: GroupMembersRequest) -> dict:
    store = get_auth_store()
    store.replace_group_members(group_id, req.user_ids)
    return {"group_id": group_id, "member_count": len(store.group_members(group_id))}


@router.post("/groups/{group_id}/members", summary="向组添加成员(owner/admin)", dependencies=[Depends(owner_required)])
def add_group_members(group_id: str, req: GroupMembersRequest) -> dict:
    store = get_auth_store()
    for uid in req.user_ids:
        store.add_group_member(group_id, uid)
    return {"group_id": group_id, "updated": True}


@router.delete("/groups/{group_id}/members/{user_id}", summary="从组移除成员(owner/admin)", dependencies=[Depends(owner_required)])
def remove_group_member(group_id: str, user_id: str) -> dict:
    get_auth_store().remove_group_member(group_id, user_id)
    return {"updated": True}