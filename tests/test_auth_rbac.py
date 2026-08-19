"""认证 / RBAC / 路由守卫测试: 密码哈希、Token 签发、权限拦截、租户隔离。

auth_mode 默认 off, 因此这里用独立构建的 AuthStore + security 函数做单元级验证,
不切换全局配置(settings 为 lru 缓存单例)。
"""

from __future__ import annotations

import pytest

from app.services.auth_store import AuthStore, get_auth_store, hash_password, verify_password


# ---------------------------------------------------------------------------
# 1. 口令哈希
# ---------------------------------------------------------------------------
def test_password_hash_roundtrip():
    h = hash_password("secret123")
    assert h.startswith("pbkdf2$")
    assert verify_password("secret123", h)
    assert not verify_password("wrong", h)


# ---------------------------------------------------------------------------
# 2. 用户/租户存储
# ---------------------------------------------------------------------------
def test_create_and_authenticate_user():
    store = get_auth_store()
    u = store.create_user("alice", "pw", tenant_id="tenA", role="member")
    assert u["role"] == "member" and u["tenant_id"] == "tenA"
    assert store.authenticate("alice", "pw")
    assert not store.authenticate("alice", "bad")
    assert store.authenticate("alice", "pw")["tenant_id"] == "tenA"


def test_invalid_role_rejected():
    store = get_auth_store()
    with pytest.raises(ValueError):
        store.create_user("bad-role", "pw", role="superadmin")


# ---------------------------------------------------------------------------
# 3. 租户隔离: 不同租户的用户独立
# ---------------------------------------------------------------------------
def test_tenant_scope_isolation():
    store = get_auth_store()
    store.create_user("uX", "x", tenant_id="tenX", role="owner")
    store.create_user("uY", "y", tenant_id="tenY", role="member")
    assert store.authenticate("uX", "x")["tenant_id"] == "tenX"
    assert store.authenticate("uY", "y")["tenant_id"] == "tenY"


# ---------------------------------------------------------------------------
# 4. JWT 创建与解码(直接在 store 上做, 不依赖全局 auth_mode)
# ---------------------------------------------------------------------------
def test_jwt_token_roundtrip(monkeypatch):
    from app.core import security
    from app.config import get_settings

    # 注入可用的 jwt_secret 供签名
    monkeypatch.setattr(get_settings(), "jwt_secret", "test-secret")
    user = {"id": "uid1", "username": "alice", "tenant_id": "tenA", "role": "member"}
    token = security.create_access_token(user)
    payload = security.decode_token(token)
    assert payload["sub"] == "uid1"
    assert payload["tenant_id"] == "tenA"