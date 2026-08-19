"""认证与授权: JWT 签发/校验, 当前用户依赖, RBAC 角色守卫, 租户解析。

演示模式(RAG_AUTH_MODE=off, 默认): 认证依赖自动放行, 返回匿名用户 + 默认租户,
行为与旧版本完全一致。生产模式(on): 需 Bearer Token, 按角色与租户隔离。

路由用法:
    from app.core.security import get_current_user, require_role, write_required
    @router.post(...)
    async def x(user: dict = Depends(get_current_user)):
        ...
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.services.auth_store import READ_ROLES, WRITE_ROLES, get_auth_store

_bearer = HTTPBearer(auto_error=False)

ANONYMOUS_ROLE = "viewer"


def _jwt_lib():
    try:
        import jwt  # PyJWT
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            500, "认证依赖缺失: 请安装 PyJWT (pip install PyJWT)"
        ) from exc
    return jwt


def _secret() -> str:
    s = get_settings().jwt_secret
    if not s:
        raise HTTPException(
            500, "RAG_AUTH_MODE=on 但未配置 RAG_JWT_SECRET"
        )
    return s


def auth_enabled() -> bool:
    return get_settings().auth_mode.lower() == "on"


def create_access_token(user: dict) -> str:
    settings = get_settings()
    jwt = _jwt_lib()
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.jwt_expire_hours)
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "tenant_id": user["tenant_id"],
        "role": user["role"],
        "exp": expire,
    }
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    settings = get_settings()
    jwt = _jwt_lib()
    try:
        payload = jwt.decode(
            token, _secret(), algorithms=[settings.jwt_algorithm]
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效或过期的凭证") from exc
    return payload


def current_tenant_id(user: Optional[dict]) -> str:
    """解析当前请求归属租户: 认证开启时用用户租户, 否则用默认租户。"""
    if user and user.get("tenant_id"):
        return user["tenant_id"]
    return get_settings().default_tenant


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """FastAPI 依赖: 返回当前用户 dict({id,username,tenant_id,role})。

    认证关闭时直接返回匿名用户, 不强制鉴权。
    """
    if not auth_enabled():
        return {
            "id": "",
            "username": "",
            "tenant_id": get_settings().default_tenant,
            "role": ANONYMOUS_ROLE,
            "is_anonymous": True,
        }
    if not credentials:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "缺少 Authorization: Bearer <token>"
        )
    payload = decode_token(credentials.credentials)
    user_id = payload.get("sub")
    store = get_auth_store()
    user = store.get_user(user_id) if user_id else None
    if not user or not user.get("is_active"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    return user


def _require_permission(allowed: set) -> Callable:
    async def checker(user: dict = Depends(get_current_user)) -> dict:
        # 认证关闭(演示模式): 不校验角色, 保持旧行为
        if not auth_enabled():
            return user
        if user.get("role") == "admin":
            return user
        if user.get("role") in allowed:
            return user
        raise HTTPException(status.HTTP_403_FORBIDDEN, "权限不足")
    return checker


# 可读(含 viewer); 可写(不含 viewer)
read_required = _require_permission(READ_ROLES)
write_required = _require_permission(WRITE_ROLES)
admin_required = _require_permission({"admin"})
owner_required = _require_permission({"admin", "owner"})


def require_tenant_resource(resource_tenant: str, user: dict) -> None:
    """校验资源租户归属, admin 可跨租户操作, 其余必须同租户。"""
    if user.get("role") == "admin":
        return
    if user.get("tenant_id") != resource_tenant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问其他租户的资源")


async def get_ws_user(websocket) -> dict:
    """WebSocket 认证: auth off 时返回匿名用户; on 时从 ?token= 或首条消息校验。"""
    if not auth_enabled():
        return {
            "id": "", "username": "", "tenant_id": get_settings().default_tenant,
            "role": ANONYMOUS_ROLE, "is_anonymous": True,
        }
    token = websocket.query_params.get("token") or ""
    if not token:
        from starlette.websockets import WebSocketDisconnect

        raise WebSocketDisconnect(code=1008)
    return decode_token(token)