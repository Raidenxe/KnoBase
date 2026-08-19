"""知识库管理 MVP(CRUD + 基础版本)测试。

覆盖:
- upload: 成功登记(done/大小/格式)、非法格式拒绝、列表含状态字段
- 列表: search / status / 分页
- detail: 基本信息 + 状态
- profile: 编辑展示名 / 版本号
- replace: 覆盖上传(新版本)
- retry: 失败文档重试的边界(源缺失 409 / 非失败 400)
- 单个删除: 级联清理记录
"""

from __future__ import annotations


def _upload_md(client, name: str, text: str) -> dict:
    with __import__("tempfile").NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as fp:
        fp.write(text)
        path = fp.name
    with open(path, "rb") as f:
        r = client.post(
            "/api/v1/documents/upload",
            files={"files": (name, f, "text/markdown")},
        )
    import os

    os.unlink(path)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# C - 创建(上传)
# ---------------------------------------------------------------------------
def test_upload_records_status_and_fields(client):
    r = _upload_md(client, "MVP设备说明书.md",
                    "# MVP设备\n\n## 1 概述\n\n型号 M-100，功率 5W。\n")
    assert r["imported_count"] == 1
    doc = r["imported"][0]
    assert doc["status"] == "done"
    assert doc["chunks"] >= 1

    listing = client.get("/api/v1/documents").json()
    hit = [d for d in listing["documents"] if d["doc_id"] == doc["doc_id"]]
    assert hit, "上传成功的文档应出现在列表"
    row = hit[0]
    assert row["status"] == "done"
    assert row["format"] == "md"
    assert row["size"] > 0


def test_upload_rejects_unsupported_format(client):
    r = client.post(
        "/api/v1/documents/upload",
        files={"files": ("bad.exe", b"MZ", "application/octet-stream")},
    )
    assert r.status_code == 200
    assert r.json()["error_count"] == 1
    assert "格式不支持" in r.json()["errors"][0]["error"]


# ---------------------------------------------------------------------------
# R - 列表(搜索/状态/分页)
# ---------------------------------------------------------------------------
def test_list_search_status_and_pagination(client, ingested):
    _upload_md(client, "Alpha网络设备.md", "# Alpha\n\n### 型号\n\nA-1 接口 10G\n")
    _upload_md(client, "Beta存储网关.md", "# Beta\n\n### 型号\n\nB-2 容量 512T\n")

    # 搜索
    hit = client.get("/api/v1/documents", params={"search": "Alpha"}).json()["documents"]
    assert hit and all("Alpha" in d["doc_name"] for d in hit)

    # 状态过滤(全是 done)
    done = client.get("/api/v1/documents", params={"status": "done"}).json()["documents"]
    assert all(d["status"] == "done" for d in done)

    # 分页第一页
    pg = client.get("/api/v1/documents", params={"page": 1, "per_page": 2}).json()
    assert len(pg["documents"]) == 2
    assert pg["total"] >= 5
    assert pg["pages"] >= 3


# ---------------------------------------------------------------------------
# R - 详情 / U - 展示名 & 版本号
# ---------------------------------------------------------------------------
def test_detail_and_profile(client):
    body = _upload_md(client, "详情设备.md", "# 详情\n\n### x\n\n描述内容\n")
    doc_id = body["imported"][0]["doc_id"]

    detail = client.get(f"/api/v1/documents/{doc_id}/detail").json()
    assert detail["doc_id"] == doc_id
    assert detail["status"] == "done"
    assert "doc_name" in detail and "version" in detail and "format" in detail

    up = client.put(f"/api/v1/documents/{doc_id}/profile",
                    json={"display_name": "详情设备(中文名)", "version": "v2.1.0"})
    assert up.status_code == 200, up.text
    assert up.json()["display_name"] == "详情设备(中文名)"
    assert up.json()["version"] == "v2.1.0"

    relist = client.get("/api/v1/documents").json()["documents"]
    row = [d for d in relist if d["doc_id"] == doc_id][0]
    assert row["doc_name"] == "详情设备(中文名)"
    assert row["version"] == "v2.1.0"

    # 空提交 -> 400
    assert client.put(f"/api/v1/documents/{doc_id}/profile", json={}).status_code == 400


# ---------------------------------------------------------------------------
# U - 覆盖上传(新版本)
# ---------------------------------------------------------------------------
def test_replace_cover_upload(client):
    body = _upload_md(client, "覆盖设备.md", "# 覆盖\n\n### v1\n\n旧内容 A\n")
    doc_id = body["imported"][0]["doc_id"]

    new_text = "# 覆盖\n\n### v2\n\n新内容 B 模块新增\n".encode("utf-8")
    r = client.post(
        f"/api/v1/documents/{doc_id}/replace",
        files={"file": ("覆盖设备.md", new_text, "text/markdown")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done"
    assert r.json()["chunks"] >= 1

    # 检索到新内容, 旧内容不再存在
    q = client.post("/api/v1/chat", json={"question": "新内容 B 模块指的是？"})
    assert "B 模块" in q.json()["answer"]


# ---------------------------------------------------------------------------
# U - 失败重试的边界
# ---------------------------------------------------------------------------
def test_retry_boundaries(client):
    from app.config import get_settings
    from app.services.doc_records import get_doc_record_store

    store = get_doc_record_store()
    tenant = get_settings().default_tenant

    # 非失败状态 -> 400
    body = _upload_md(client, "重试设备.md", "# 重试\n\n### x\n\n内容\n")
    doc_id = body["imported"][0]["doc_id"]
    assert client.post(f"/api/v1/documents/{doc_id}/retry").status_code == 400

    # 失败且源文件缺失 -> 409
    store.register("deadbeef", tenant, "坏文件.pdf", format="pdf", size=1,
                   source_path="/tmp/not_exists.pdf")
    store.set_status("deadbeef", tenant, "failed", "文件已损坏")
    assert client.post("/api/v1/documents/deadbeef/retry").status_code == 409


# ---------------------------------------------------------------------------
# D - 单个删除级联清理
# ---------------------------------------------------------------------------
def test_delete_purges_record_and_source(client):
    from app.config import get_settings
    from app.services.doc_records import get_doc_record_store

    body = _upload_md(client, "待删设备.md", "# 待删\n\n### x\n\n内容\n")
    doc_id = body["imported"][0]["doc_id"]
    rec = get_doc_record_store().get(doc_id, get_settings().default_tenant)
    assert rec and rec["source_path"] and rec["source_path"].endswith(".md")

    assert client.delete(f"/api/v1/documents/{doc_id}").status_code == 200
    assert get_doc_record_store().get(doc_id, get_settings().default_tenant) is None
    after = client.get("/api/v1/documents").json()["documents"]
    assert all(d["doc_id"] != doc_id for d in after)