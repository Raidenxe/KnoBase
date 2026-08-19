"""WebSocket 实时问答 + 全链路追踪记录测试。"""

from __future__ import annotations

import json

from app.services.trace_store import get_trace_store


def test_ws_chat_roundtrip(client):
    with client.websocket_connect("/api/v1/ws/chat") as ws:
        ws.send_text(json.dumps({"question": "DataGate 网关怎么进入配置界面？"}))
        events = []
        for _ in range(50):
            try:
                msg = ws.receive_json()
            except Exception:  # noqa: BLE001
                break
            events.append(msg["event"])
            if msg["event"] in ("done", "error"):
                assert "trace_id" in msg["data"]
                break
    assert "done" in events or "error" in events
    # 流式链路应能见到 status/token/done
    assert "token" in events or "status" in events


def test_trace_recorded_after_chat(client):
    r = client.post("/api/v1/chat", json={"question": "网关的默认登录信息是什么？"})
    assert r.status_code == 200
    trace_id = r.json().get("trace_id", "")
    assert trace_id

    record = get_trace_store().get(trace_id)
    assert record is not None
    assert record["status"] == "ok"
    stages = {s["stage"] for s in record["spans"]}
    # 核心链路至少包含检索与生成
    assert "retrieve" in stages
    assert "generate" in stages

    # 观测接口可读
    detail = client.get(f"/api/v1/traces/{trace_id}")
    assert detail.status_code == 200
    assert detail.json()["trace_id"] == trace_id
    basic = client.get("/api/v1/metrics/basic")
    assert basic.status_code == 200
    assert "stages" in basic.json()