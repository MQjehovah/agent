"""Task 6：记忆审批路由测试。

包含两类测试：
1. 存储层行为测试 —— 验证 approve/reject 的存储层效果（路由本质是 storage 操作的薄封装）。
2. HTTP 路由测试 —— 通过 Flask test_client 端到端验证路由注册与响应。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import storage as storage_mod
from storage import Storage, get_storage

# ===== 存储层行为测试（验证 approve/reject 的业务语义） =====


def test_approve_flow_moves_proposal_to_global(tmp_path):
    """审批通过的 proposal 应写入 global 记忆，对所有用户可见"""
    s = Storage(str(tmp_path))
    pid = s.save_proposal(content="通用知识X", reason="常识")
    p = s.get_proposal(pid)
    # 模拟 approve 路由的存储层动作
    s.save_memory(scope="global", owner_id="", category="knowledge",
                  content=p["content"], source="admin")
    s.update_proposal_status(pid, "approved", "admin")
    # global 记忆对所有用户可见
    rows = s.query_memories(user_id="anyUser")
    assert any(r["content"] == "通用知识X" and r["scope"] == "global" for r in rows)
    # proposal 状态已变
    assert s.get_proposal(pid)["status"] == "approved"
    s.close()


def test_reject_flow_keeps_no_global(tmp_path):
    """驳回的 proposal 不应写入 global"""
    s = Storage(str(tmp_path))
    pid = s.save_proposal(content="不该公开的", reason="误判")
    s.update_proposal_status(pid, "rejected", "admin")
    rows = s.query_memories(user_id="anyUser")
    assert not any(r["content"] == "不该公开的" for r in rows)
    s.close()


# ===== HTTP 路由测试（通过 Flask test_client 端到端验证） =====


def _make_app(tmp_path):
    """构造 WebServer 的 FastAPI app 并注入临时 storage 单例，返回 (client, restore)"""
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s

    from fastapi.testclient import TestClient

    from web.server import WebServer

    w = WebServer()
    client = TestClient(w._app)

    def restore():
        s.close()
        storage_mod._storage_instance = prev

    return client, restore


def test_route_list_proposals_default_pending(tmp_path):
    """GET /api/memory/proposals 默认返回 pending proposals"""
    client, restore = _make_app(tmp_path)
    try:
        get_storage().save_proposal(content="待审A", reason="测试")
        resp = client.get("/api/memory/proposals")
        assert resp.status_code == 200
        contents = [p["content"] for p in resp.json()["proposals"]]
        assert "待审A" in contents
    finally:
        restore()


def test_route_list_proposals_filter_status(tmp_path):
    """?status= 可指定过滤，approved 不出现在 pending 默认列表"""
    client, restore = _make_app(tmp_path)
    try:
        s = get_storage()
        pid = s.save_proposal(content="已审B", reason="测试")
        s.update_proposal_status(pid, "approved", "admin")

        # 默认 pending 不含已审B
        resp = client.get("/api/memory/proposals")
        assert "已审B" not in [p["content"] for p in resp.json()["proposals"]]

        # 显式查 approved
        resp = client.get("/api/memory/proposals?status=approved")
        assert "已审B" in [p["content"] for p in resp.json()["proposals"]]
    finally:
        restore()


def test_route_approve_writes_global_and_marks_approved(tmp_path):
    """POST .../approve 写入 global 记忆并标记 approved"""
    client, restore = _make_app(tmp_path)
    try:
        s = get_storage()
        pid = s.save_proposal(content="需公开知识", reason="通用")

        resp = client.post(f"/api/memory/proposals/{pid}/approve")
        assert resp.status_code == 200
        assert resp.json() == {"success": True}

        # proposal 已 approved
        assert s.get_proposal(pid)["status"] == "approved"
        # 已写入 global，对所有用户可见
        rows = s.query_memories(user_id="someone")
        assert any(r["content"] == "需公开知识" and r["scope"] == "global" for r in rows)
    finally:
        restore()


def test_route_approve_rejects_non_pending(tmp_path):
    """非 pending 的 proposal 不可重复审批，返回 400"""
    client, restore = _make_app(tmp_path)
    try:
        s = get_storage()
        pid = s.save_proposal(content="重复审", reason="测试")
        s.update_proposal_status(pid, "approved", "admin")

        resp = client.post(f"/api/memory/proposals/{pid}/approve")
        assert resp.status_code == 400
    finally:
        restore()


def test_route_approve_missing_proposal_400(tmp_path):
    """不存在的 proposal 返回 400"""
    client, restore = _make_app(tmp_path)
    try:
        resp = client.post("/api/memory/proposals/9999/approve")
        assert resp.status_code == 400
    finally:
        restore()


def test_route_reject_marks_rejected_no_global(tmp_path):
    """POST .../reject 标记 rejected 且不写入 global"""
    client, restore = _make_app(tmp_path)
    try:
        s = get_storage()
        pid = s.save_proposal(content="驳回项", reason="误判")

        resp = client.post(f"/api/memory/proposals/{pid}/reject")
        assert resp.status_code == 200
        assert resp.json() == {"success": True}

        # proposal 已 rejected
        assert s.get_proposal(pid)["status"] == "rejected"
        # 未写入 global
        rows = s.query_memories(user_id="someone")
        assert not any(r["content"] == "驳回项" for r in rows)
    finally:
        restore()
