"""MCP 运行时治理(W2)测试: 每 server 可配 / 状态聚合 / 故障清单 / 健康端点。

不真连 stdio: 用 monkeypatch 的 fake stdio_client + FakeSessionCtx 模拟连接;
端点用 FastAPI TestClient + monkeypatch MCPManager.status_all 校验响应形状。
"""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from fastapi.testclient import TestClient

import mcps.manager as manager


def _install_fake_stdio(monkeypatch, fail_args_containing: str | None = None):
    """fake stdio + session: args 命中标记则连接抛错, 否则返回含 1 个工具的 session。"""
    tool = SimpleNamespace(name="echo", description="回声", input_schema={"type": "object"})
    session = SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[tool])),
    )

    class FakeSessionCtx:
        def __init__(self, read_stream, write_stream):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc_info):
            return False

    @asynccontextmanager
    async def fake_stdio_client(server, errlog=None):
        if fail_args_containing and any(fail_args_containing in a for a in server.args):
            raise RuntimeError("子进程启动失败")
        yield (object(), object())

    monkeypatch.setattr(manager, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(manager, "ClientSession", FakeSessionCtx)


def _write_config(tmp_path, entries: list[dict]) -> str:
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _rows(status: dict, prefix: str) -> dict[str, dict]:
    return {r["name"]: r for r in status["servers"] if r["name"].startswith(prefix)}


# ===== 1. 每 server 配置解析 =====

def test_config_defaults_when_fields_absent():
    conn = manager.MCPServerConnection("w2_demo", {})
    assert conn.timeout_seconds == 60
    assert conn.connect_timeout_seconds == 30
    assert conn.max_reconnect_attempts == 3
    assert conn.max_concurrency == 4


def test_config_custom_values():
    conn = manager.MCPServerConnection("w2_demo", {
        "timeout_seconds": 300.5,
        "connect_timeout_seconds": 5,
        "max_reconnect_attempts": 7,
        "max_concurrency": 2,
    })
    assert conn.timeout_seconds == 300.5
    assert conn.connect_timeout_seconds == 5
    assert conn.max_reconnect_attempts == 7
    assert conn.max_concurrency == 2


@pytest.mark.parametrize("bad", ["abc", 0, -1, True])
def test_config_invalid_tool_timeout_falls_back(bad):
    conn = manager.MCPServerConnection("w2_demo", {"timeout_seconds": bad})
    assert conn.timeout_seconds == 60


@pytest.mark.parametrize("bad", ["abc", 0, -1, True])
def test_config_invalid_connect_timeout_falls_back(bad):
    conn = manager.MCPServerConnection("w2_demo", {"connect_timeout_seconds": bad})
    assert conn.connect_timeout_seconds == 30


@pytest.mark.parametrize("bad", ["abc", -1, True])
def test_config_invalid_reconnect_attempts_falls_back(bad):
    conn = manager.MCPServerConnection("w2_demo", {"max_reconnect_attempts": bad})
    assert conn.max_reconnect_attempts == 3


@pytest.mark.parametrize("bad", ["abc", 0, -2, True])
def test_config_invalid_concurrency_falls_back(bad):
    conn = manager.MCPServerConnection("w2_demo", {"max_concurrency": bad})
    assert conn.max_concurrency == 4


# ===== 2. 状态与故障清单 =====

async def test_manager_status_isolates_failure_and_lists_failed(tmp_path, monkeypatch, caplog):
    _install_fake_stdio(monkeypatch, fail_args_containing="w2_bad.py")
    cfg = _write_config(tmp_path, [
        {"name": "w2_good", "enabled": True, "command": "python", "args": ["w2_good.py"],
         "timeout_seconds": 12, "max_concurrency": 2},
        {"name": "w2_bad", "enabled": True, "command": "python", "args": ["w2_bad.py"]},
        {"name": "w2_off", "enabled": False, "command": "python", "args": ["w2_off.py"]},
    ])
    mgr = manager.MCPManager(cfg)
    await mgr.connect()
    try:
        assert set(mgr.servers) == {"w2_good"}  # 单 server 失败不影响其它
        mgr.servers["w2_good"]._reconnect_attempts = 2

        data = mgr.status()
        rows = _rows(data, "w2_")
        assert set(rows) == {"w2_good", "w2_bad", "w2_off"}
        assert rows["w2_good"]["connected"] is True
        assert rows["w2_good"]["tools"] == 1
        assert rows["w2_good"]["reconnects"] == 2
        assert rows["w2_good"]["timeout_seconds"] == 12
        assert rows["w2_good"]["max_concurrency"] == 2
        assert rows["w2_good"]["last_error"] == ""
        assert rows["w2_bad"]["connected"] is False
        assert "子进程启动失败" in rows["w2_bad"]["last_error"]
        assert rows["w2_off"]["enabled"] is False
        assert data["summary"] == {"connected": 1, "failed": 1, "total": 3}
        assert any("MCP [w2_bad] 连接失败: 子进程启动失败" in r.message for r in caplog.records)
    finally:
        await mgr.close()


async def test_status_all_merges_instances_and_unregisters_on_close(tmp_path, monkeypatch):
    _install_fake_stdio(monkeypatch)
    cfg = _write_config(tmp_path, [
        {"name": "w2_time", "enabled": True, "command": "python", "args": ["w2_time.py"]},
    ])
    a = manager.MCPManager(cfg)
    b = manager.MCPManager(cfg)
    await a.connect()
    await b.connect()
    try:
        rows = _rows(manager.MCPManager.status_all(), "w2_time")
        assert rows["w2_time"]["connected"] is True
        assert rows["w2_time"]["tools"] == 1
    finally:
        await a.close()
        await b.close()
    # close 后注册表注销, 不再出现在聚合状态里
    assert _rows(manager.MCPManager.status_all(), "w2_time") == {}


async def test_connect_server_failure_appears_in_status(tmp_path, monkeypatch):
    """生产 Agent 路径走 connect_server(动态): 失败也要进失败清单。"""
    _install_fake_stdio(monkeypatch, fail_args_containing="w2_dyn_bad.py")
    mgr = manager.MCPManager("")
    try:
        assert await mgr.connect_server({
            "name": "w2_dyn_bad", "enabled": True, "command": "python", "args": ["w2_dyn_bad.py"]}) is False
        data = mgr.status()
        row = _rows(data, "w2_dyn_bad")["w2_dyn_bad"]
        assert row["connected"] is False and "子进程启动失败" in row["last_error"]
        assert data["summary"]["failed"] == 1
    finally:
        await mgr.close()


async def test_last_error_truncated_to_limit():
    conn = manager.MCPServerConnection("w2_demo", {})
    conn._set_last_error("错误" * 500)
    assert manager.MCP_LAST_ERROR_MAX_LEN == 200
    assert len(conn.last_error) == manager.MCP_LAST_ERROR_MAX_LEN


async def test_call_tool_failure_records_last_error():
    async def failing(name, args):
        raise RuntimeError("boom")

    conn = manager.MCPServerConnection("w2_demo", {})
    conn.session = SimpleNamespace(call_tool=failing)
    conn._connected = True
    out = await conn.call_tool("t", {})
    assert out.startswith("执行失败")
    assert "RuntimeError" in conn.last_error and "boom" in conn.last_error


async def test_reconnect_counts_and_respects_limit(monkeypatch):
    monkeypatch.setattr(manager, "MCP_RECONNECT_DELAY", 0)
    conn = manager.MCPServerConnection("w2_demo", {"max_reconnect_attempts": 1})
    conn.connect = AsyncMock(return_value=False)
    assert await conn.reconnect() is False
    assert conn.reconnect_attempts == 1
    assert await conn.reconnect() is False  # 达上限, 不再尝试
    assert conn.reconnect_attempts == 1

    ok = manager.MCPServerConnection("w2_demo_ok", {})
    ok.connect = AsyncMock(return_value=True)
    assert await ok.reconnect() is True
    assert ok.reconnect_attempts == 0


# ===== 3. 超时与并发生效 =====

async def test_call_tool_timeout_uses_configured_timeout():
    async def slow(name, args):
        await asyncio.sleep(1)
        return SimpleNamespace(content=[SimpleNamespace(text="迟到")])

    conn = manager.MCPServerConnection("w2_demo", {"timeout_seconds": 0.2})
    conn.session = SimpleNamespace(call_tool=slow)
    conn._connected = True

    loop = asyncio.get_running_loop()
    started = loop.time()
    out = await conn.call_tool("t", {})
    elapsed = loop.time() - started
    assert out == "执行失败: 工具调用超时"
    assert elapsed < 0.9  # 0.2s 覆盖硬编码 60s(否则这里会等 1s 并成功)
    assert "工具调用超时" in conn.last_error


async def test_max_concurrency_one_serializes_queued_calls():
    order = []

    async def record(name, args):
        order.append(f"enter-{args['n']}")
        await asyncio.sleep(0.05)
        order.append(f"exit-{args['n']}")
        return SimpleNamespace(content=[SimpleNamespace(text="ok")])

    conn = manager.MCPServerConnection("w2_demo", {"max_concurrency": 1})
    assert conn.max_concurrency == 1
    conn.session = SimpleNamespace(call_tool=record)
    conn._connected = True

    results = await asyncio.gather(conn.call_tool("t", {"n": 1}), conn.call_tool("t", {"n": 2}))
    assert results == ["ok", "ok"]
    assert order == ["enter-1", "exit-1", "enter-2", "exit-2"]


# ===== 4. /healthz 与 /api/admin/mcp =====

def _fake_status_all() -> dict:
    return {
        "servers": [
            {"name": "w2_time", "enabled": True, "connected": True, "tools": 3, "last_error": "",
             "reconnects": 0, "timeout_seconds": 60.0, "max_concurrency": 4},
            {"name": "w2_terminal", "enabled": True, "connected": False, "tools": 0,
             "last_error": "连接失败: RuntimeError: boom", "reconnects": 3,
             "timeout_seconds": 300.0, "max_concurrency": 4},
            {"name": "w2_fs", "enabled": False, "connected": False, "tools": 0,
             "last_error": "未连接", "reconnects": 0, "timeout_seconds": 60.0, "max_concurrency": 4},
        ],
        "summary": {"connected": 1, "failed": 1, "total": 3},
    }


@pytest.fixture
def web_client(monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")
    monkeypatch.setattr(manager.MCPManager, "status_all", staticmethod(_fake_status_all))
    from web.server import WebServer
    return TestClient(WebServer()._app)


def test_healthz_appends_mcp_summary_and_failed(web_client):
    resp = web_client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mcp"]["summary"] == {"connected": 1, "failed": 1, "total": 3}
    assert body["mcp"]["failed"] == ["w2_terminal"]  # 禁用的 server 不算故障
    assert {"status", "now", "uptime_s", "db", "pool"} <= set(body)  # 原结构兼容


def test_admin_mcp_returns_full_status(web_client):
    resp = web_client.get("/api/admin/mcp")
    assert resp.status_code == 200
    assert resp.json() == _fake_status_all()


def test_admin_mcp_requires_monitor_permission(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    import storage.storage as storage_mod
    from storage.storage import Storage
    from web.server import WebServer, create_jwt

    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    try:
        client = TestClient(WebServer()._app)
        h = {"Authorization": f"Bearer {create_jwt({'id': 7, 'name': '用户', 'role': 'default'})}"}
        assert client.get("/api/admin/mcp", headers=h).status_code == 403
    finally:
        s.close()
        storage_mod._storage_instance = prev
