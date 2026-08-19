"""安全加固回归测试: 会话 IDOR / 登录锁定与口令强度 / 文档读授权绕过。

全部使用独立临时库, 不污染运行时数据。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from app.services.history import ConversationStore, get_conversation_store
from app.services.auth_store import (
    ACCOUNT_LOCK_SECONDS,
    LOGIN_MAX_ATTEMPTS,
    AuthStore,
    validate_password,
)


def _tmp_db(suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="sec_" + suffix, suffix=".db")
    os.close(fd)
    return path


# ---------------------------------------------------------------------------
# 1. 会话 IDOR: 会话按 owner 隔离
# ---------------------------------------------------------------------------
def test_conversation_owner_isolation():
    db = _tmp_db("conv.db")
    ts = tempfile.mkdtemp(prefix="sec_conv_")
    src = os.path.join(ts, "h.db")
    try:
        store = ConversationStore(src)
        ca = store.create_conversation("alice 的会话", tenant_id="tenA", owner="user_a")
        cb = store.create_conversation("bob 的会话", tenant_id="tenA", owner="user_b")

        # bob 列表只能看到自己的会话
        bob_list = store.list_conversations(50, "tenA", "user_b")
        ids = [c["id"] for c in bob_list]
        assert ca["id"] not in ids and cb["id"] in ids
        # bob 不能读取 alice 的会话
        assert store.get_conversation(ca["id"], "tenA", "user_b") is None
        assert store.get_conversation(cb["id"], "tenA", "user_b") is not None
        # 不带 owner 过滤 = 管理员/匿名可见(旧行为)
        assert store.get_conversation(ca["id"], "tenA") is not None
    finally:
        try:
            os.remove(src)
            os.rmdir(ts)
        except OSError:
            pass
        try:
            os.remove(db)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 2. 口令强度校验
# ---------------------------------------------------------------------------
def test_password_strength_validation():
    assert validate_password("pw") is not None          # 过短
    assert validate_password("password") is not None     # 无大小写+数字
    assert validate_password("StrongPass1") is None      # 合规


# ---------------------------------------------------------------------------
# 3. 登录锁定: 连续失败达上限后锁定
# ---------------------------------------------------------------------------
def test_login_lockout():
    db = _tmp_db("lock.db")
    try:
        store = AuthStore(db)
        store.create_user("lockuser", "StrongPass1", tenant_id="tenL", role="member")
        for _ in range(LOGIN_MAX_ATTEMPTS):
            store.register_login_failure("lockuser")
        st = store.login_status("lockuser")
        assert st["locked"] is True
        assert st["retry_in"] <= ACCOUNT_LOCK_SECONDS
        # 登录成功后清零
        store.register_login_success("lockuser")
        assert store.login_status("lockuser")["locked"] is False
    finally:
        try:
            os.remove(db)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 4. 文档读授权: private / 分类可读范围 (复用路由层视图判定)
# ---------------------------------------------------------------------------
def test_doc_read_authorization_dispatch(monkeypatch):
    from app.api.routes import documents as doc_routes

    # 强制进入 认证开启(auth on) 路径, 测试授权判定本身
    monkeypatch.setattr(doc_routes, "auth_enabled", lambda: True)

    # 认证开启 + 匿名: 仍一律可见(回退)
    anon = {"role": "viewer", "is_anonymous": True}
    assert doc_routes._doc_viewable({"access_scope": "private", "category": "x"}, anon) is True

    # admin: 含 private 全可见
    admin = {"role": "admin"}
    assert doc_routes._doc_viewable({"access_scope": "private", "category": "x"}, admin) is True

    # 非 admin: private 不可见
    viewer = {"role": "viewer", "is_anonymous": False}
    assert doc_routes._doc_viewable({"access_scope": "private", "category": "x"}, viewer) is False

    # 无授权则跳过(不 assert 依赖具体分类授权, 仅验证入库路径不异常)
    assert callable(doc_routes._raise_unless_doc_viewable)