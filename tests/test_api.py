"""API 集成测试: 问答 / 流式 / 引用 / 拒答 / 文档与会话管理 / 性能指标

运行: pytest tests/test_api.py -v
"""

import json


def test_health(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("healthy", "degraded")
    assert body["components"]["milvus"]["status"] == "up"


def test_chat_with_citations(client):
    resp = client.post(
        "/api/v1/chat", json={"question": "SmartOps 默认管理员账号和初始密码是什么？"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert "Admin@2024" in body["answer"] or "admin" in body["answer"]
    assert body["citations"], "必须携带来源引用"
    c = body["citations"][0]
    assert c["doc_name"] == "智慧运维管理平台产品说明书"
    assert "3 安装部署" in c["section_path"] or "默认账号" in c["section_path"]
    assert c["chunk_id"]


def test_chat_performance_targets(client):
    """性能指标: 检索 <500ms, 总体 <3s"""
    resp = client.post("/api/v1/chat", json={"question": "网关 RUN 灯常亮代表什么？"})
    metrics = resp.json()["metrics"]
    assert metrics["retrieval_ms"] < 500, f"检索超时: {metrics}"
    assert metrics["total_ms"] < 3000, f"总延迟超标: {metrics}"


def test_chat_stream_sse(client):
    with client.stream(
        "POST", "/api/v1/chat/stream", json={"question": "升级前如何备份？"}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = []
        buffer = ""
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                event, data = None, {}
                for line in raw.split("\n"):
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:].strip())
                events.append((event, data))
    names = [e for e, _ in events]
    assert "token" in names, "必须有流式 token 事件"
    assert "done" in names
    done = next(d for e, d in events if e == "done")
    assert done["citations"], "done 事件必须携带引用"
    assert done["metrics"]["retrieval_ms"] < 500


def test_out_of_scope_refusal(client):
    resp = client.post("/api/v1/chat", json={"question": "今天股市行情怎么样？"})
    body = resp.json()
    assert body["citations"] == []
    assert "无法回答" in body["answer"]


def test_multi_turn_conversation(client):
    r1 = client.post("/api/v1/chat", json={"question": "SmartOps 的告警收敛窗口是多久？"})
    conv_id = r1.json()["conversation_id"]
    r2 = client.post(
        "/api/v1/chat",
        json={"question": "那它的服务热线是多少？", "conversation_id": conv_id},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert "400-800-1234" in body["answer"]


def test_documents_listing_and_delete(client):
    resp = client.get("/api/v1/documents")
    docs = resp.json()["documents"]
    assert len(docs) >= 3
    doc_id = docs[0]["doc_id"]
    resp = client.delete(f"/api/v1/documents/{doc_id}")
    assert resp.status_code == 200
    after = client.get("/api/v1/documents").json()["documents"]
    assert all(d["doc_id"] != doc_id for d in after)
    # 重新导入恢复
    resp = client.post("/api/v1/documents/scan", json={})
    assert resp.status_code == 200


def test_conversation_history(client):
    r = client.post("/api/v1/chat", json={"question": "UniAuth 令牌有效期是多久？"})
    conv_id = r.json()["conversation_id"]
    detail = client.get(f"/api/v1/conversations/{conv_id}")
    assert detail.status_code == 200
    msgs = detail.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["citations"]

    listing = client.get("/api/v1/conversations")
    assert any(c["id"] == conv_id for c in listing.json()["conversations"])

    assert client.delete(f"/api/v1/conversations/{conv_id}").status_code == 200
    assert client.get(f"/api/v1/conversations/{conv_id}").status_code == 404


def test_upload_document(client, tmp_path):
    manual = tmp_path / "测试设备说明书.md"
    manual.write_text(
        "# 测试设备\n\n## 1 概述\n\n测试设备型号 TX-100，功率 5W。\n\n## 2 维保\n\n质保 2 年。\n",
        encoding="utf-8",
    )
    with manual.open("rb") as f:
        resp = client.post(
            "/api/v1/documents/upload",
            files={"files": ("测试设备说明书.md", f, "text/markdown")},
        )
    assert resp.status_code == 200
    assert resp.json()["imported_count"] == 1

    r = client.post("/api/v1/chat", json={"question": "TX-100 测试设备的功率是多少？"})
    assert "5W" in r.json()["answer"]
