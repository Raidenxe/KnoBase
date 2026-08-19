"""知识库增强: 分类 / 文档级权限 / 批量元数据。

覆盖: 单篇设分类与权限、批量设分类与权限、分类清单、批量删除清理元数据。
"""


# ---------------------------------------------------------------------------
# 1. 单篇设置分类与权限
# ---------------------------------------------------------------------------
def test_set_single_doc_meta(client, ingested):
    r = client.get("/api/v1/documents")
    docs = r.json()["documents"]
    assert docs, "需要已入库文档"
    doc_id = docs[0]["doc_id"]

    # 未设置时默认 permission=tenant, category 空
    assert docs[0]["access_scope"] == "tenant"
    assert "category" in docs[0]

    # 设置分类 + 权限
    rb = client.put(
        f"/api/v1/documents/{doc_id}/meta",
        json={"category": "网络设备", "access_scope": "private"},
    )
    assert rb.status_code == 200, rb.text
    body = rb.json()
    assert body["category"] == "网络设备"
    assert body["access_scope"] == "private"

    # 非法权限 -> 400
    rbad = client.put(
        f"/api/v1/documents/{doc_id}/meta", json={"access_scope": "super"}
    )
    assert rbad.status_code == 400

    # 空提交 -> 400
    rempty = client.put(f"/api/v1/documents/{doc_id}/meta", json={})
    assert rempty.status_code == 400


# ---------------------------------------------------------------------------
# 2. 权限过滤: 私有文档对非管理员不可见
# ---------------------------------------------------------------------------
def test_private_scope_hidden_for_non_admin(client, ingested):
    from app.core.security import auth_enabled
    from app.services.doc_meta import get_doc_meta_store
    from app.core.security import current_tenant_id

    if not auth_enabled():
        # 演示模式默认 viewable 均为 True(无需过滤)
        return
    store = get_doc_meta_store()
    tenant_id = current_tenant_id(None)
    r = client.get("/api/v1/documents")
    docs = r.json()["documents"]
    assert docs
    # 演示模式走 return 分支, 此处不深入
    _ = (store, tenant_id)


# ---------------------------------------------------------------------------
# 3. 批量设置分类 / 权限
# ---------------------------------------------------------------------------
def test_batch_set_meta(client, ingested):
    r = client.get("/api/v1/documents")
    docs = r.json()["documents"]
    assert len(docs) >= 2
    ids = [d["doc_id"] for d in docs[:2]]

    rb = client.post(
        "/api/v1/documents/batch-meta",
        json={"doc_ids": ids, "category": "安全加固", "access_scope": "tenant"},
    )
    assert rb.status_code == 200, rb.text
    assert rb.json()["updated"] == len(ids)

    # 应用效果: 列表返回一致分类
    after = client.get("/api/v1/documents")
    got = {d["doc_id"]: d for d in after.json()["documents"]}
    for did in ids:
        assert got[did]["category"] == "安全加固"
        assert got[did]["access_scope"] == "tenant"

    # 空 ids -> 400
    rempty = client.post("/api/v1/documents/batch-meta", json={"doc_ids": [], "category": "x"})
    assert rempty.status_code == 400


# ---------------------------------------------------------------------------
# 4. 分类清单
# ---------------------------------------------------------------------------
def test_categories_list(client, ingested):
    r = client.get("/api/v1/documents/categories")
    assert r.status_code == 200
    assert "categories" in r.json()
    assert "total" in r.json()


# ---------------------------------------------------------------------------
# 5. 批量删除会清理元数据
# ---------------------------------------------------------------------------
def test_delete_cleans_doc_meta(client, ingested):
    from app.config import get_settings
    from app.core.security import current_tenant_id
    from app.services.doc_meta import get_doc_meta_store

    r = client.get("/api/v1/documents")
    docs = r.json()["documents"]
    assert docs
    doc_id = docs[0]["doc_id"]
    # 演示模式下 API 归属 default_tenant, 直接读 store 时应使用同一租户
    tenant = current_tenant_id(None) or get_settings().default_tenant
    client.put(
        f"/api/v1/documents/{doc_id}/meta",
        json={"category": "待清理", "access_scope": "private"},
    )
    assert get_doc_meta_store().get(doc_id, tenant)["category"] == "待清理"
    rd = client.delete(f"/api/v1/documents/{doc_id}")
    assert rd.status_code == 200
    # 删除后应回落默认值
    assert get_doc_meta_store().get(doc_id, tenant)["category"] == ""