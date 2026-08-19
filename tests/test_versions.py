"""文档版本管理测试: 版本写入递增 / 列表 / 快照 / API 端点。"""

from __future__ import annotations

from app.services.versions import get_version_store


def _chunks(n=3):
    return [{"chunk_index": i, "section_path": "S", "page": 1, "text": f"内容 {i}"}
            for i in range(n)]


def test_save_version_increments_and_dedups():
    store = get_version_store()
    doc = "ver-test-1"
    v1 = store.save_version(doc, "doc", "fp-1", 3, "a.md", _chunks(), "default", "alice")
    assert v1 == 1
    # 指纹相同 → 复用不新增
    v1b = store.save_version(doc, "doc", "fp-1", 3, "a.md", _chunks(), "default", "alice")
    assert v1b == v1
    # 指纹变化 → +1
    v2 = store.save_version(doc, "doc", "fp-2", 3, "a.md", _chunks(), "default", "alice")
    assert v2 == 2


def test_list_and_detail():
    store = get_version_store()
    doc = "ver-test-2"
    store.save_version(doc, "doc", "fp-1", 2, "b.md", _chunks(2), "default", "bob")
    store.save_version(doc, "doc", "fp-2", 2, "b.md", _chunks(2), "default", "bob")
    versions = store.list_versions(doc)
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # 倒序
    snap = store.get_version(doc, 2)
    assert snap and len(snap["chunks"]) == 2
    assert snap["chunks"][0]["text"] == "内容 0"


def test_version_api_endpoints(client):
    # 先经入库落一个版本记录(用示例文件路径由 ingest 产生)
    import os
    from pathlib import Path

    from app.knowledge.pipeline import IngestPipeline

    fixture = Path(os.environ["RAG_MANUALS_DIR"]) / "DataGate数据采集网关产品说明书.md"
    r = IngestPipeline().ingest_file(fixture, created_by="tester")
    doc_id = r["doc_id"]

    # 版本列表
    resp = client.get(f"/api/v1/documents/{doc_id}/versions")
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert versions and versions[0]["chunk_count"] > 0

    # 指定版本详情
    v = versions[0]["version"]
    detail = client.get(f"/api/v1/documents/{doc_id}/versions/{v}")
    assert detail.status_code == 200
    assert detail.json()["chunks"]

    # 回滚到该版本(生成新修订)
    rb = client.post(f"/api/v1/documents/{doc_id}/versions/{v}/rollback")
    assert rb.status_code == 200
    assert rb.json()["current_version"] > v
    assert rb.json()["chunk_count"] > 0