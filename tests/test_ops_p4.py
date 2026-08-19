"""第四批 P2 功能测试: 接口性能监控 / 提示词热更新 / 操作审计 / LLM 用量。

覆盖:
- /metrics/endpoints 中间件埋点聚合(P99/QPS/请求量)
- /admin/prompt 查看/热更新/恢复内置默认
- /audit 审计日志(上传文档会落一条敏感操作)
- /metrics/usage LLM token 用量与成本核算结构
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 1. 接口性能监控: 中间件埋点 + 聚合统计
# ---------------------------------------------------------------------------
def test_endpoint_metrics_records_and_aggregates(client, ingested):
    # 先制造若干 /api 请求, 触发中间件埋点
    for _ in range(3):
        assert client.get("/api/v1/documents").status_code == 200

    r = client.get("/api/v1/metrics/endpoints")
    assert r.status_code == 200
    body = r.json()
    assert body["total_requests"] >= 1
    assert body["window_seconds"] >= 1
    paths = {e["path"] for e in body["endpoints"]}
    assert "/api/v1/documents" in paths
    for e in body["endpoints"]:
        assert e["count"] >= 1
        assert e["qps"] >= 0
        assert e["p99_ms"] >= 0
        # 分位应单调合理(排序后 p50<=p95<=p99)
        assert e["p50_ms"] <= e["p95_ms"] <= e["p99_ms"] <= e["max_ms"]


# ---------------------------------------------------------------------------
# 2. 提示词热更新: 查看 / 保存即时生效 / 恢复默认
# ---------------------------------------------------------------------------
def test_prompt_get_returns_effective(client):
    r = client.get("/api/v1/admin/prompt")
    assert r.status_code == 200
    body = r.json()
    assert "customized" in body
    assert "builtin_prompt" in body
    assert "effective_prompt" in body
    assert body["effective_prompt"]  # 内置默认非空
    assert body["customized"] is False


def test_prompt_hot_update_and_reset(client):
    new_prompt = "你是智能维保助手, 请严格依据产品说明书作答。"
    # 保存自定义提示词
    r = client.put("/api/v1/admin/prompt", json={"prompt": new_prompt})
    assert r.status_code == 200, r.text
    assert r.json()["customized"] is True

    # 即时生效: 重新读取为自定义值
    g = client.get("/api/v1/admin/prompt").json()
    assert g["customized"] is True
    assert g["effective_prompt"] == new_prompt

    # 空提示词应 400
    assert client.put("/api/v1/admin/prompt", json={"prompt": "   "}).status_code == 400

    # 恢复内置默认
    rs = client.put("/api/v1/admin/prompt", json={"reset": True})
    assert rs.status_code == 200, rs.text
    assert rs.json()["customized"] is False
    assert client.get("/api/v1/admin/prompt").json()["customized"] is False


# ---------------------------------------------------------------------------
# 3. 操作审计日志: 敏感操作被记录
# ---------------------------------------------------------------------------
def test_audit_records_sensitive_operations(client, ingested):
    before = len(client.get("/api/v1/audit").json()["logs"])

    # 触发一次上传(敏感操作), 应新增审计日志
    import os
    from pathlib import Path

    fp = Path(os.environ["RAG_MANUALS_DIR"]) / "DataGate数据采集网关产品说明书.md"
    with open(fp, "rb") as f:
        files = {"files": ("upload_test.md", f.read(), "text/markdown")}
        r = client.post("/api/v1/documents/upload", files=files)
    assert r.status_code == 200, r.text

    logs = client.get("/api/v1/audit").json()["logs"]
    assert len(logs) > before
    assert logs[0]["action"] == "upload"
    assert logs[0]["target"]  # 文档名/文档 id 非空


def test_audit_routes_shape(client):
    r = client.get("/api/v1/audit?limit=100")
    assert r.status_code == 200
    body = r.json()
    assert "logs" in body
    for log in body["logs"]:
        for k in ("time", "actor", "action", "target", "detail"):
            assert k in log


# ---------------------------------------------------------------------------
# 4. LLM 用量: token 消耗与成本核算结构
# ---------------------------------------------------------------------------
def test_llm_usage_structure(client, ingested):
    r = client.get("/api/v1/metrics/usage?hours=24")
    assert r.status_code == 200
    body = r.json()
    for k in ("hours", "estimated_cost", "totals", "by_model"):
        assert k in body
    assert body["hours"] == 24
    # mock 模式不产生真实计费, by_model 可为空, 但字段结构必须完整
    for m in body["by_model"]:
        for k in ("model", "calls", "prompt_tokens", "completion_tokens",
                  "total_tokens", "estimated_cost"):
            assert k in m


def test_llm_usage_store_record_and_summary(client):
    """直接驱动 store 验证 token 汇总与费用计算(不依赖真实 LLM)。"""
    from app.services.llm_usage import get_llm_usage_store

    store = get_llm_usage_store()
    store.record("test-model", "mock", prompt_tokens=1000, completion_tokens=2000)
    s = store.summary(hours=24)
    assert s["totals"]["calls"] >= 1
    assert s["totals"]["total_tokens"] >= 3000
    assert s["by_model"][0]["model"] == "test-model"
    assert s["estimated_cost"] >= 0