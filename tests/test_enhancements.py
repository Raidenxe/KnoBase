"""新增能力测试: 模型切换鲁棒性 / 增量入库 / 后台任务 / 网页导入参数校验"""

from __future__ import annotations

import time

from app.core.llm import LLMService


# ---------------------------------------------------------------------------
# 1. 判定输出解析(切模型兼容性)
# ---------------------------------------------------------------------------
def test_parse_yes_no_variants():
    cases = {
        "yes": True,
        "Yes, the chunk is relevant.": True,
        "no": False,
        "NO": False,
        "该片段与问题相关": True,
        "不相关": False,
        "无关": False,
        "是的，可以帮助回答": True,
    }
    for raw, expected in cases.items():
        assert LLMService._parse_yes_no(raw) is expected, raw


def test_extract_json_with_fences():
    assert LLMService._extract_json('```json\n{"supported": true}\n```').get("supported") is True
    assert LLMService._extract_json('结果如下 {"supported": false} 完毕').get("supported") is False


# ---------------------------------------------------------------------------
# 2. 宽松一致性校验(改写通过 / 幻觉拦截)
# ---------------------------------------------------------------------------
def test_verify_allows_paraphrase_but_blocks_hallucination():
    blocks = [
        {"index": 1, "doc_name": "网关说明书", "section_path": "5 配置", "text": "默认用户名 admin，默认密码 gateway。首次登录后必须修改密码。"}
    ]
    # 改写(关键词保留) → 通过
    ok = LLMService._containment_verify(
        "该设备的默认账号为 admin，初始密码是 gateway [1]", blocks
    )
    assert ok.supported
    # 编造(资料中不存在) → 拦截
    bad = LLMService._containment_verify(
        "该设备的初始密码是 P@ssw0rd999，且支持指纹解锁 [1]", blocks
    )
    assert not bad.supported


# ---------------------------------------------------------------------------
# 3. 增量入库: 未变化文档跳过
# ---------------------------------------------------------------------------
def test_incremental_skip(ingested):
    import os
    from pathlib import Path

    from app.knowledge.pipeline import IngestPipeline

    pipeline = IngestPipeline()
    fixture = Path(os.environ["RAG_MANUALS_DIR"]) / "DataGate数据采集网关产品说明书.md"
    r1 = pipeline.ingest_file(fixture)                      # 已在 ingested 中, 强制重导
    assert r1["chunks"] > 0
    r2 = pipeline.ingest_file(fixture, skip_existing=True)  # 增量 → 跳过
    assert r2.get("skipped") is True


# ---------------------------------------------------------------------------
# 4. 后台任务与网页导入接口
# ---------------------------------------------------------------------------
def test_task_not_found(client):
    assert client.get("/api/v1/tasks/nonexistent").status_code == 404


def test_web_import_validation(client):
    assert client.post("/api/v1/documents/web-import", json={"urls": []}).status_code == 400
    # 非法协议: 不崩溃, 返回错误明细
    r = client.post(
        "/api/v1/documents/web-import", json={"urls": ["ftp://invalid.example/x.md"]}
    )
    assert r.status_code == 200
    assert r.json()["error_count"] == 1


def test_background_scan_returns_task(client):
    r = client.post("/api/v1/documents/scan?background=true", json={})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    for _ in range(30):
        t = client.get(f"/api/v1/tasks/{task_id}").json()
        if t["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert t["status"] == "done"
