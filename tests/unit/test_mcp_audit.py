"""W2b 测试: MCP 调用审计(mcp_calls 表 + executor 写入点 + admin 查询端点)。

- storage: record_mcp_call/query_mcp_calls(真实临时 SQLite, 过滤/倒序/limit 夹紧/截断);
- executor: execute_tool 的 MCP 分支测量耗时并写审计(成功/失败/异常; 非 MCP 不写);
- 端点: /api/admin/mcp/calls 形状与权限(TestClient + monkeypatch)。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from fastapi.testclient import TestClient

from storage.storage import MCP_CALL_ERROR_MAX_LEN, MCP_CALL_QUERY_MAX_LIMIT, Storage

# ===== 1. storage 落库与查询 =====

def test_record_and_query_mcp_calls(tmp_path):
    s = Storage(str(tmp_path))
    try:
        rid1 = s.record_mcp_call(
            server="srv1", tool="t1", exposed="t1", ok=True, duration_ms=12,
            result_chars=3, conversation_id="web:7:abc", user_id="web:7",
            channel="web", agent_name="根代理")
        rid2 = s.record_mcp_call(
            server="srv2", tool="t2", exposed="srv2__t2", ok=False, duration_ms=34,
            result_chars=0, error="执行失败: boom", conversation_id="dingtalk:9:x",
            user_id="dingtalk:9", channel="dingtalk", agent_name="设备运维",
            ts="2026-01-01T00:00:00")
        assert rid2 > rid1 > 0

        rows = s.query_mcp_calls()
        assert [r["id"] for r in rows] == [rid2, rid1]  # 按 id 倒序
        row = rows[0]
        assert row["server"] == "srv2" and row["tool"] == "t2"
        assert row["exposed"] == "srv2__t2" and row["ok"] == 0
        assert row["duration_ms"] == 34 and row["error"] == "执行失败: boom"
        assert row["conversation_id"] == "dingtalk:9:x" and row["user_id"] == "dingtalk:9"
        assert row["channel"] == "dingtalk" and row["agent_name"] == "设备运维"

        assert [r["tool"] for r in s.query_mcp_calls(server="srv1")] == ["t1"]
        assert [r["tool"] for r in s.query_mcp_calls(tool="t2")] == ["t2"]
        assert [r["tool"] for r in s.query_mcp_calls(tool="缺失")] == []
        assert len(s.query_mcp_calls(since="2020-01-01")) == 2
        assert len(s.query_mcp_calls(since="2030-01-01")) == 0
    finally:
        s.close()


def test_record_mcp_call_truncates_error(tmp_path):
    s = Storage(str(tmp_path))
    try:
        s.record_mcp_call(error="错" * (MCP_CALL_ERROR_MAX_LEN + 200))
        row = s.query_mcp_calls(limit=1)[0]
        assert len(row["error"]) == MCP_CALL_ERROR_MAX_LEN
    finally:
        s.close()


def test_query_mcp_calls_limit_clamped(tmp_path):
    s = Storage(str(tmp_path))
    try:
        with s.get_connection() as conn:
            conn.executemany(
                "INSERT INTO mcp_calls (ts, server, tool) VALUES (?, 's', 't')",
                [(f"2026-01-01T00:00:{i % 60:02d}",) for i in range(MCP_CALL_QUERY_MAX_LIMIT + 5)],
            )
            conn.commit()
        assert len(s.query_mcp_calls(limit=10 ** 6)) == MCP_CALL_QUERY_MAX_LIMIT
        assert len(s.query_mcp_calls(limit=0)) == 1
    finally:
        s.close()


def test_prune_mcp_calls_removes_old_rows(tmp_path):
    """保留策略: 超过 keep_days 的审计删除, 幂等; 非法 keep_days 不清理。"""
    s = Storage(str(tmp_path))
    try:
        s.record_mcp_call(server="s", tool="old", ts="2020-01-01T00:00:00")
        s.record_mcp_call(server="s", tool="new")
        assert s.prune_mcp_calls(keep_days=30) == 1
        assert [r["tool"] for r in s.query_mcp_calls()] == ["new"]
        assert s.prune_mcp_calls(keep_days=30) == 0
        assert s.prune_mcp_calls(keep_days=0) == 0
        assert s.prune_mcp_calls(keep_days=-1) == 0
    finally:
        s.close()


# ===== 2. executor 写入点 =====

class _FakeMCP:
    def __init__(self, result="完成", risk="read", raise_exc=False):
        self.result = result
        self.risk = risk
        self.raise_exc = raise_exc

    def has_tool(self, name):
        return name.startswith("raw_")

    def tool_risk(self, name):
        return self.risk if self.has_tool(name) else None

    def tool_server(self, name):
        return "srv"

    def tool_raw(self, name):
        return name

    async def call_tool(self, name, args):
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.result


class _FakeStorage:
    def __init__(self):
        self.calls = []

    def record_mcp_call(self, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


def _fake_tool_agent(mcp):
    return SimpleNamespace(
        mcp=mcp, tool_registry=None, skill_manager=None, plugin_manager=None,
        storage=_FakeStorage(),
    )


async def test_execute_tool_records_success_call():
    from agent.core import RunContext, _current_run
    from agent.executor import execute_tool

    agent = _fake_tool_agent(_FakeMCP(result="查询结果"))
    rc = RunContext(user_id="web:7", conversation_id="web:7:abc", agent_id="根")
    tok = _current_run.set(rc)
    try:
        out = await execute_tool(agent, "raw_query", {"q": "1"})
    finally:
        _current_run.reset(tok)

    assert out == "查询结果"
    rec = agent.storage.calls[0]
    assert rec["server"] == "srv" and rec["tool"] == "raw_query" and rec["exposed"] == "raw_query"
    assert rec["ok"] is True and rec["error"] == ""
    assert rec["result_chars"] == len("查询结果") and rec["duration_ms"] >= 0
    assert rec["conversation_id"] == "web:7:abc" and rec["user_id"] == "web:7"
    assert rec["channel"] == "web" and rec["agent_name"] == "根"


async def test_execute_tool_records_failure_result():
    from agent.executor import execute_tool

    agent = _fake_tool_agent(_FakeMCP(result="执行失败: 工具调用超时", risk="destructive"))
    out = await execute_tool(agent, "raw_send", {})
    assert out.startswith("执行失败")
    rec = agent.storage.calls[0]
    assert rec["ok"] is False
    assert rec["error"] == "执行失败: 工具调用超时"
    assert rec["server"] == "srv"


async def test_execute_tool_records_raised_exception():
    from agent.executor import execute_tool

    agent = _fake_tool_agent(_FakeMCP(raise_exc=True))
    out = await execute_tool(agent, "raw_boom", {})
    assert out == "工具执行错误: boom"  # execute_tool 外层兜底, 审计在抛出点记录
    rec = agent.storage.calls[0]
    assert rec["ok"] is False and rec["error"] == "boom" and rec["result_chars"] == 0


async def test_execute_tool_skips_audit_for_non_mcp_tool():
    """非 MCP 工具(has_tool=False)不写审计; 真 MCP 工具 has_tool 为真时风险必非 None。"""
    from agent.executor import execute_tool

    class _NonMCP:
        def has_tool(self, name):
            return False

    agent = _fake_tool_agent(_NonMCP())
    out = await execute_tool(agent, "raw_plain", {})
    assert "不存在" in out
    assert agent.storage.calls == []


def test_channel_falls_back_to_user_id_prefix():
    from agent.core import RunContext, _current_run
    from agent.executor import _record_mcp_call

    agent = _fake_tool_agent(_FakeMCP())
    rc = RunContext(user_id="dingtalk:9", conversation_id="")
    tok = _current_run.set(rc)
    try:
        _record_mcp_call(agent, "raw_x", 5, result="ok")
    finally:
        _current_run.reset(tok)
    assert agent.storage.calls[0]["channel"] == "dingtalk"


# ===== 3. admin 端点 =====

_ROWS = [
    {"id": 2, "ts": "2026-01-02T00:00:00", "server": "srv1", "tool": "t1",
     "exposed": "t1", "ok": 1, "duration_ms": 5, "result_chars": 3, "error": "",
     "conversation_id": "web:7:abc", "user_id": "web:7", "channel": "web", "agent_name": "根"},
]


@pytest.fixture
def web_client(monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")
    from web.server import WebServer
    return TestClient(WebServer()._app)


def test_admin_mcp_calls_shape_and_filters(web_client, monkeypatch):
    captured = {}

    class FakeStorage:
        def query_mcp_calls(self, limit=100, server=None, tool=None, since=None):
            captured.update(limit=limit, server=server, tool=tool, since=since)
            return _ROWS

    import storage.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_storage", lambda: FakeStorage())

    resp = web_client.get("/api/admin/mcp/calls?limit=50&server=srv1&tool=t1")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"calls": _ROWS, "count": 1}
    assert captured == {"limit": 50, "server": "srv1", "tool": "t1", "since": None}


def test_admin_mcp_calls_defaults_pass_none_filters(web_client, monkeypatch):
    captured = {}

    class FakeStorage:
        def query_mcp_calls(self, limit=100, server=None, tool=None, since=None):
            captured.update(limit=limit, server=server, tool=tool)
            return []

    import storage.storage as storage_mod
    monkeypatch.setattr(storage_mod, "get_storage", lambda: FakeStorage())

    resp = web_client.get("/api/admin/mcp/calls")
    assert resp.status_code == 200
    assert resp.json() == {"calls": [], "count": 0}
    assert captured == {"limit": 100, "server": None, "tool": None}


def test_admin_mcp_calls_requires_monitor_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    import storage.storage as storage_mod
    from storage.storage import Storage as RealStorage
    from web.server import WebServer, create_jwt

    prev = storage_mod._storage_instance
    s = RealStorage(str(tmp_path))
    storage_mod._storage_instance = s
    try:
        client = TestClient(WebServer()._app)
        h = {"Authorization": f"Bearer {create_jwt({'id': 7, 'name': '用户', 'role': 'default'})}"}
        assert client.get("/api/admin/mcp/calls", headers=h).status_code == 403
    finally:
        s.close()
        storage_mod._storage_instance = prev


# ===== 4. 数据范围过滤(department 范围仅本部门成员) =====

def _seed_dept_scope_users(st):
    """建 设备部/售后部 与 deptmon 角色(admin.monitor + data_scope=department)。"""
    from security.rbac import RBACManager

    rbac = RBACManager(st)
    rbac.create_department("设备部")
    rbac.create_department("售后部")
    rbac.create_role("deptmon", permissions=["admin.monitor"], data_scope="department")
    mgr = rbac.create_user(name="mgr", department="设备部", role="deptmon", display_name="设备主管")
    a1 = rbac.create_user(name="a1", department="设备部", display_name="设备甲")
    b1 = rbac.create_user(name="b1", department="售后部", display_name="售后乙")
    return mgr, a1, b1


def test_admin_mcp_calls_department_scope_filters(tmp_path, monkeypatch):
    """department 范围管理员只看到本部门成员记录; admin 全站(含系统调用)。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    import storage.storage as storage_mod
    from storage.storage import Storage as RealStorage
    from web.server import WebServer, create_jwt

    prev = storage_mod._storage_instance
    s = RealStorage(str(tmp_path))
    storage_mod._storage_instance = s
    try:
        mgr, a1, b1 = _seed_dept_scope_users(s)
        s.record_mcp_call(server="srv", tool="t", user_id=f"web:{a1}", ok=True)
        s.record_mcp_call(server="srv", tool="t", user_id=f"web:{b1}", ok=True)
        s.record_mcp_call(server="srv", tool="t", user_id=f"web:{mgr}", ok=True)
        s.record_mcp_call(server="srv", tool="t", user_id="", ok=True)  # 系统调用(无归属)

        client = TestClient(WebServer()._app)
        h = {"Authorization": f"Bearer {create_jwt({'id': mgr, 'name': 'mgr', 'role': 'deptmon'})}"}
        body = client.get("/api/admin/mcp/calls", headers=h).json()
        assert body["count"] == 2
        assert {c["user_id"] for c in body["calls"]} == {f"web:{a1}", f"web:{mgr}"}

        admin = {"Authorization": f"Bearer {create_jwt({'id': 1, 'name': 'admin', 'role': 'admin'})}"}
        body = client.get("/api/admin/mcp/calls", headers=admin).json()
        assert body["count"] == 4
        assert {c["user_id"] for c in body["calls"]} == {
            f"web:{a1}", f"web:{b1}", f"web:{mgr}", ""}
    finally:
        s.close()
        storage_mod._storage_instance = prev
