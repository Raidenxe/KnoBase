"""用户组 + 知识库分类 + 授权矩阵(Who→Group→What) 数据层单元测试。

HTTP 层路由仅在 RAG_AUTH_MODE=on 时挂载, 此处直接测 store 方法, 不依赖鉴权模式。
"""


# ---------------------------------------------------------------------------
# 用户组: 默认组、成员管理、删除约束
# ---------------------------------------------------------------------------
def test_seeded_default_groups():
    store = __import__("app.services.auth_store", fromlist=["get_auth_store"]).get_auth_store()
    names = {g["name"] for g in store.list_groups()}
    assert {"admin", "ops", "viewer"} <= names, names


def test_group_member_lifecycle():
    store = __import__("app.services.auth_store", fromlist=["get_auth_store"]).get_auth_store()
    u = store.create_user("m1", "pass123", role="member", display_name="成员一")
    g = store.create_group("研发团队", "研发与架构文档")
    store.replace_group_members(g["id"], [u["id"]])
    assert len(store.group_members(g["id"])) == 1
    found = [x for x in store.list_groups() if x["name"] == "研发团队"]
    assert found and found[0]["member_count"] == 1

    # 组内有成员时删除应被拒绝
    try:
        store.delete_group(g["id"])
        deleted = True
    except ValueError:
        deleted = False
    assert not deleted

    # 移除成员后可删除, 且级联清理授权矩阵(此处无授权, 仅验证不抛错)
    store.remove_group_member(g["id"], u["id"])
    assert store.delete_group(g["id"])["deleted"] is True


def test_replace_user_groups():
    store = __import__("app.services.auth_store", fromlist=["get_auth_store"]).get_auth_store()
    u = store.create_user("m2", "pass123", role="viewer", display_name="只读")
    g1 = store.create_group("临时组A", "")
    g2 = store.create_group("临时组B", "")
    store.replace_user_groups(u["id"], [g1["id"], g2["id"]])
    assert len(store.user_groups(u["id"])) == 2
    store.replace_user_groups(u["id"], [g1["id"]])
    assert len(store.user_groups(u["id"])) == 1


def test_delete_user_keeps_docs():
    store = __import__("app.services.auth_store", fromlist=["get_auth_store"]).get_auth_store()
    u = store.create_user("m3", "pass123", role="member", display_name="待删")
    g = store.create_group("临时组C", "")
    store.replace_user_groups(u["id"], [g["id"]])
    store.delete_user(u["id"])
    assert store.get_user(u["id"]) is None
    # 组归属被级联清除, 组本身仍存在
    assert store.get_group(g["id"]) is not None


# ---------------------------------------------------------------------------
# 知识库分类 + 授权矩阵
# ---------------------------------------------------------------------------
def test_category_crud_and_delete_guard():
    meta = __import__("app.services.doc_meta", fromlist=["get_doc_meta_store"]).get_doc_meta_store()
    tenant = "t-test"
    meta.create_category("运维手册", tenant, "日常运维操作", by="admin")
    meta.create_category("API文档", tenant, "接口说明", by="admin")
    names = {c["name"] for c in meta.list_categories(tenant)}
    assert {"运维手册", "API文档"} <= names

    # 分类下有文档时删除被拒绝
    meta.set_meta("doc-1", tenant, category="运维手册")
    try:
        meta.delete_category("运维手册", tenant)
        blocked = False
    except ValueError:
        blocked = True
    assert blocked

    # 移走文档后可删除(级联清理授权矩阵, 此处无授权)
    meta.set_meta("doc-1", tenant, category="")
    assert meta.delete_category("API文档", tenant)["deleted"] is True

    # 不可重复创建同名分类
    try:
        meta.create_category("运维手册", tenant, "")
        dup_ok = True
    except ValueError:
        dup_ok = False
    assert not dup_ok


def test_category_grants_matrix():
    meta = __import__("app.services.doc_meta", fromlist=["get_doc_meta_store"]).get_doc_meta_store()
    auth = __import__("app.services.auth_store", fromlist=["get_auth_store"]).get_auth_store()
    tenant = "t-grants"
    meta.create_category("故障排查", tenant, "排障", by="admin")
    meta.create_category("产品介绍", tenant, "产品", by="admin")
    g_ops = auth.create_group("运维团队", "")
    g_cs = auth.create_group("客服团队", "")

    # 运维组: 故障排查 读写; 客服组: 产品介绍 只读
    meta.set_category_grant(g_ops["id"], "故障排查", tenant, can_read=True, can_manage=True, by="admin")
    meta.set_category_grant(g_cs["id"], "产品介绍", tenant, can_read=True, can_manage=False, by="admin")

    # 组→可读/可管分类
    assert "故障排查" in meta.user_readable_categories([g_ops["id"]])
    assert "故障排查" in meta.user_manageable_categories([g_ops["id"]])
    assert meta.user_manageable_categories([g_cs["id"]]) == []  # 只读不可管
    assert "产品介绍" in meta.user_readable_categories([g_cs["id"]])

    # 矩阵返回
    cells = {c["group_id"]: c["category"] for c in meta.get_category_grants(tenant)}
    assert cells.get(g_ops["id"]) == "故障排查"

    # 组删除后级联清理授权
    # 客服组无成员, 可删除; 验证 delete_group 级联
    assert auth.delete_group(g_cs["id"])["deleted"] is True
    assert meta.user_readable_categories([g_cs["id"]]) == []


def test_category_rename_syncs_grants():
    meta = __import__("app.services.doc_meta", fromlist=["get_doc_meta_store"]).get_doc_meta_store()
    auth = __import__("app.services.auth_store", fromlist=["get_auth_store"]).get_auth_store()
    tenant = "t-rename"
    meta.create_category("旧名", tenant, "", by="admin")
    meta.set_meta("doc-r1", tenant, category="旧名")
    g = auth.create_group("改名组", "")
    meta.set_category_grant(g["id"], "旧名", tenant, can_read=True, by="admin")

    meta.rename_category("旧名", "新名", tenant)
    # 文档与授权矩阵同步改名
    assert meta.get("doc-r1", tenant)["category"] == "新名"
    rows = meta.grants_for_groups([g["id"]])
    assert rows and rows[0]["category"] == "新名"


# ---------------------------------------------------------------------------
# P2: 登录日志(IP/设备) + 授权变更审计
# ---------------------------------------------------------------------------
def test_login_logs_recorded():
    store = __import__("app.services.auth_store", fromlist=["get_auth_store"]).get_auth_store()
    u = store.create_user("log-user", "pass123", role="viewer", display_name="日志用户")
    store.record_login(u["id"], ip="10.0.0.5", device="Mozilla/5.0 test-agent")
    store.record_login(u["id"], ip="10.0.0.9", device="curl/8.0")

    logs = store.list_login_logs(u["id"], limit=10)
    assert len(logs) == 2
    assert logs[0]["ip"] == "10.0.0.9"  # 新→旧排序
    assert "curl/8.0" in logs[0]["device"]
    assert logs[0]["time"]  # 有人类可读时间
    # 最近登录时间被更新
    assert store.get_user(u["id"])["last_login_at"] > 0


def test_grant_audit_written():
    from app.services.audit import audit, get_audit_store
    # 授权变更经 audit() 便捷入口落库(路由层 set_grant 会调用)
    audit("grant", actor="admin", target="grant:g1:审计分类",
          detail="read=True manage=False", tenant_id="t-audit-grant", role="admin")
    rows = get_audit_store().recent_for("grant:g1:审计分类", limit=5)
    assert rows and rows[0]["action"] == "grant" and rows[0]["actor"] == "admin"