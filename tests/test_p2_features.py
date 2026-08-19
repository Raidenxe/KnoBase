"""第三批 P2 功能测试: 引用溯源 / 会话导出与分享 / 知识库命中统计 / 批量删除。

覆盖:
- /chat 返回 citations(含 section_path/chunk_id) 与 suggestions(追问)
- /conversations/{id}/export 导出 Markdown/Text
- /conversations/{id}/export/share 生成分享链接 + /api/v1/shares/{token} 只读
- 分享撤销 DELETE /conversations/share/{token}
- /documents/stats 每文档命中覆盖统计
- /documents/batch-delete 批量删除文档
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def conv_id(client):
    """创建并完成一轮对话, 返回 conversation_id 与响应体(供后续复用)。"""
    r = client.post(
        "/api/v1/chat",
        json={"question": "SmartOps 平台管理员账号默认是什么？", "conversation_id": None},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body


# ---------------------------------------------------------------------------
# 1. 引用溯源: citations 携带章节/块定位字段 + 追问建议
# ---------------------------------------------------------------------------
def test_chat_returns_citations_and_suggestions(conv_id):
    assert conv_id["conversation_id"]
    assert isinstance(conv_id["citations"], list)
    assert isinstance(conv_id["suggestions"], list)
    # Mock 抽取式回答会带 [n] 引用标号, 因而应有引用
    assert len(conv_id["citations"]) >= 1, "mock 回答应产生引用"
    for c in conv_id["citations"]:
        assert "index" in c
        assert "chunk_id" in c, "引用须含 chunk_id 用于章节锚点定位"
        assert "doc_id" in c
        assert "section_path" in c
        assert "snippet" in c


def test_chat_suggestions_are_clickable_questions(conv_id):
    # 追问来自引用文档, 应为非空且长度合理的字符串
    for s in conv_id["suggestions"]:
        assert isinstance(s, str) and 2 <= len(s) <= 80


# ---------------------------------------------------------------------------
# 2. 会话导出
# ---------------------------------------------------------------------------
def test_export_markdown(conv_id, client):
    r = client.get(f"/api/v1/conversations/{conv_id['conversation_id']}/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "智能软件维保助手" in r.text or "SmartOps" in r.text


def test_export_text(conv_id, client):
    r = client.get(f"/api/v1/conversations/{conv_id['conversation_id']}/export", params={"format": "text"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


def test_export_unknown_format_defaults_markdown(conv_id, client):
    # 导出的默认/兜底格式为 Markdown(仅 text 走纯文本), 未知格式不报错
    r = client.get(f"/api/v1/conversations/{conv_id['conversation_id']}/export", params={"format": "xlsx"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")


# ---------------------------------------------------------------------------
# 3. 会话分享: 生成 / 只读查看 / 撤销
# ---------------------------------------------------------------------------
def test_share_lifecycle(conv_id, client):
    # 生成分享链接
    rs = client.post(f"/api/v1/conversations/{conv_id['conversation_id']}/export/share", json={})
    assert rs.status_code == 200, rs.text
    token = rs.json()["token"]
    assert rs.json()["url"].startswith("/s/")

    # 公开只读查看快照
    rg = client.get(f"/api/v1/shares/{token}")
    assert rg.status_code == 200
    data = rg.json()
    assert data["token"] == token
    assert any(m["role"] == "user" for m in data["messages"])
    assert any(m["role"] == "assistant" for m in data["messages"])

    # 撤销后不可查看
    assert client.delete(f"/api/v1/conversations/share/{token}").status_code == 200
    assert client.get(f"/api/v1/shares/{token}").status_code == 404


def test_share_ttl_expiry(client):
    # 不可手动等待, 仅验证 ttl 校验边界(>60s 合法)
    cid = {"id": None}
    # 用空会话逻辑在 400 校验; ttl 边界交由 pydantic: 59 -> 422
    r = client.post("/api/v1/conversations/nope/export/share", json={"ttl_seconds": 59})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 4. 知识库命中统计
# ---------------------------------------------------------------------------
def test_documents_stats_reports_hits(client, ingested):
    r = client.get("/api/v1/documents/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_documents"] >= 1
    assert "coverage_rate" in body
    # hits 仅在有会话命中后 >0, 这里至少结构正确
    assert all("hits" in d and "chunks" in d and "doc_id" in d for d in body["documents"])


# ---------------------------------------------------------------------------
# 5. 批量删除
# ---------------------------------------------------------------------------
def test_batch_delete_restores_state(client, ingested):
    """批量删除后验证清单; 随后重新导入被删文档, 避免影响同 session 其他测试模块。"""
    import os
    from pathlib import Path

    from app.knowledge.pipeline import IngestPipeline

    r = client.get("/api/v1/documents")
    docs = r.json()["documents"]
    assert len(docs) >= 2
    ids = [d["doc_id"] for d in docs[:2]]

    rb = client.post("/api/v1/documents/batch-delete", json={"doc_ids": ids})
    assert rb.status_code == 200, rb.text
    assert rb.json()["count"] == len(ids)
    assert set(rb.json()["deleted"]) == set(ids)

    after = client.get("/api/v1/documents").json()["documents"]
    remaining = {d["doc_id"] for d in after}
    assert not set(ids) & remaining

    # 恢复: 重新导入示例说明书, 保持后续测试模块的文档基线
    fixtures = Path(os.environ["RAG_MANUALS_DIR"])
    for f in fixtures.glob("*"):
        if f.is_file() and f.suffix.lower() in {".md", ".mdx", ".txt", ".pdf", ".docx"}:
            IngestPipeline().ingest_file(f)


def test_batch_delete_empty_400(client):
    r = client.post("/api/v1/documents/batch-delete", json={"doc_ids": []})
    assert r.status_code == 400