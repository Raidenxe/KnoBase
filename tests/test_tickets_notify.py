"""工单系统 + 通知中心回归测试。

工单(API 层, auth off): 提交/列表/详情/回复/状态流转(admin)/确认关闭/重新打开/统计
通知(存储层单元测试): 推送/最近列表/未读数/已读/删除/广播
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 工单: 端到端生命周期
# ---------------------------------------------------------------------------
def _create_ticket(client, urgency="urgent", ntype="知识库缺失"):
    r = client.post(
        "/api/v1/tickets",
        data={"ticket_type": ntype, "title": "K3s 高可用部署文档缺失",
              "description": "搜索不到相关文档，希望补充。", "urgency": urgency},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"].startswith("TK")
    assert body["status"] == "pending"
    assert "已提交" in body["message"]
    return body["id"]


def test_ticket_create_and_list(client):
    tid = _create_ticket(client)
    r = client.get("/api/v1/tickets")
    assert r.status_code == 200
    d = r.json()
    assert d["total"] >= 1
    hits = [t for t in d["tickets"] if t["id"] == tid]
    assert hits, "新建工单应出现在我的工单列表"
    t = hits[0]
    assert t["status"] == "pending"
    assert t["urgency"] == "urgent"
    assert t["creator_name"] is not None
    # 列表项应精简(不含 replies)
    assert "replies" not in t


def test_ticket_detail_empty_replies(client):
    tid = _create_ticket(client, urgency="normal")
    r = client.get(f"/api/v1/tickets/{tid}")
    assert r.status_code == 200
    t = r.json()
    assert t["title"] == "K3s 高可用部署文档缺失"
    assert t["status"] == "pending"
    assert isinstance(t["replies"], list)
    assert t["replies"] == []


def test_ticket_user_reply_then_admin_flow(client):
    tid = _create_ticket(client, urgency="normal")
    # 用户补充一条回复
    r1 = client.post(f"/api/v1/tickets/{tid}/replies",
                     json={"content": "补充：需要包含 etcd 部署"})
    assert r1.status_code == 200, r1.text

    # 管理端流转: 待处理 -> 处理中 -> 已解决(带说明)
    for st, note in [("processing", "开始处理"), ("resolved", "已上传《K3s高可用部署指南》")]:
        rr = client.post(f"/api/v1/tickets/admin/{tid}/status",
                         json={"status": st, "note": note})
        assert rr.status_code == 200, rr.text
        assert rr.json()["status"] == st

    # 管理端回复
    r2 = client.post(f"/api/v1/tickets/admin/{tid}/reply",
                     json={"content": "可重新检索试试。"})
    assert r2.status_code == 200, r2.text

    # 详情应含用户回复 + 管理员回复, 时间线倒序
    det = client.get(f"/api/v1/tickets/{tid}").json()
    assert len(det["replies"]) == 2
    assert det["status"] == "resolved"
    assert det["handle_note"] == "已上传《K3s高可用部署指南》"


def test_ticket_confirm_close_and_reopen(client):
    tid = _create_ticket(client, urgency="normal")
    client.post(f"/api/v1/tickets/admin/{tid}/status", json={"status": "resolved"})
    # 确认关闭
    rc = client.post(f"/api/v1/tickets/{tid}/close", json={})
    assert rc.status_code == 200, rc.text
    assert rc.json()["status"] == "closed"
    # 关闭后可重新打开(7 天内)
    rr = client.post(f"/api/v1/tickets/{tid}/reopen", json={})
    assert rr.status_code == 200, rr.text
    assert rr.json()["status"] == "processing"


def test_ticket_admin_list_and_stats(client):
    _create_ticket(client, urgency="urgent")
    rl = client.get("/api/v1/tickets/admin")
    assert rl.status_code == 200
    ld = rl.json()
    assert ld["total"] >= 1
    assert "type_options" in ld
    assert ld["type_options"], "须返回可选工单类型"
    # 类型筛选
    rf = client.get("/api/v1/tickets/admin", params={"ticket_type": "知识库缺失"})
    assert rf.status_code == 200

    rs = client.get("/api/v1/tickets/admin/stats")
    assert rs.status_code == 200
    sd = rs.json()
    assert "total" in sd
    assert "avg_handle_hours" in sd
    assert "by_type" in sd
    assert sd["by_type"], "按类型分布应有数据"


# ---------------------------------------------------------------------------
# 通知: 存储层生命周期
# ---------------------------------------------------------------------------
def _notify_store():
    from app.services.notify_store import get_notify_store
    return get_notify_store()


def test_notify_push_read_delete():
    from app.services.notify_store import T_KB_UPDATE, T_TICKET_STATUS
    store = _notify_store()
    tenant = "notify_test_tenant"
    uid = "notify_user_1"
    nid = store.notify_user(tenant, uid, T_TICKET_STATUS, "工单 TK1 状态变更",
                            content="已变更为【已解决】", link="/tickets.html?tk=TK1")
    assert nid
    # 广播
    n2 = store.make_broadcast(tenant, ["u_a", "u_b"], T_KB_UPDATE,
                              "知识库更新", "新增部署指南", "/documents-browser")
    assert n2 == 2

    recent = store.recent(tenant, uid, n=5)
    assert any(r["id"] == nid for r in recent)
    assert store.unread_count(tenant, uid) == 1
    # 标记已读
    assert store.mark_read(nid, uid) is True
    assert store.unread_count(tenant, uid) == 0
    # 删除
    assert store.delete(nid, uid) is True
    assert store.recent(tenant, uid, n=5) == []


def test_notify_ownership_isolation():
    store = _notify_store()
    tenant = "notify_test_tenant2"
    nid = store.notify_user(tenant, "owner_x", "permission", "权限更新", "新增读取权限")
    # 其它用户(非 owner_x)不可读取/标记/删除
    assert store.get(nid, "attacker") is None
    assert store.mark_read(nid, "attacker") is False
    assert store.delete(nid, "attacker") is False
    assert store.recent(tenant, "attacker") == []


def test_notify_list_filter_by_type():
    from app.services.notify_store import T_ANNOUNCE, T_TICKET_STATUS
    store = _notify_store()
    tenant = "notify_test_tenant3"
    uid = "u_filter"
    store.notify_user(tenant, uid, T_TICKET_STATUS, "工单状态", "1")
    store.notify_user(tenant, uid, T_ANNOUNCE, "系统公告", "2")
    lst = store.list_notifications(tenant, uid, ntype=T_ANNOUNCE)
    assert all(x["type"] == T_ANNOUNCE for x in lst)
    assert store.unread_count(tenant, uid) >= 2


# ---------------------------------------------------------------------------
# 项目文档(admin 在线浏览): 列表 + 内容
# ---------------------------------------------------------------------------
def test_project_docs_list(client):
    r = client.get("/api/v1/project-docs")
    assert r.status_code == 200, r.text
    d = r.json()
    names = [x["name"] for x in d["docs"]]
    assert "README.md" in names, "列表应包含根 README.md"
    assert any(n.endswith(".md") for n in names), "列表应包含 docs/ 下的 markdown"
    assert d["total"] == len(d["docs"])


def test_project_docs_content(client):
    # 读取 README, 应返回 Markdown 原文
    r = client.get("/api/v1/project-docs/README.md")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "README.md"
    assert "# KnoBase RAG智能助手" in body["content"]

    # 读取 docs 目录某文档(工单与通知.md 为本项目文档)
    lst = client.get("/api/v1/project-docs").json()["docs"]
    doc = next((x for x in lst if x["name"] == "工单与通知.md"), None)
    if doc:
        r2 = client.get("/api/v1/project-docs/" + "工单与通知.md")
        assert r2.status_code == 200
        assert "通知中心" in r2.json()["content"]


def test_project_docs_security(client):
    # 非法名(路径穿越或不存在)不应 200 返回内容
    for p in ("..%2F..%2Fetc%2Fpasswd", "../README.md", "no_such_file.md"):
        r = client.get("/api/v1/project-docs/" + p)
        # 正确的拒绝(400 非法名 / 404 不存在 / 422 路由解析)均可接受
        assert r.status_code in (400, 404, 422), (p, r.status_code)