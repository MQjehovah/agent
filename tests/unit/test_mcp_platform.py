"""平台 MCP 轨(市场能力目录 sync + /relay 直连)测试。

分层验证:
- 纯函数: 配置解析 / sync 过滤 / 暴露名 / relay URL;
- PlatformMCPClient 集成: httpx.MockTransport 假 sync + 注入假 session
  → 连接注册/工具路由/风险映射/失败隔离/刷新下线;
- 与 MCPManager 合并: duck 假平台 → 工具表/风险/分发/本地优先/状态汇总/审计归属;
- /healthz: mcp.platform 汇总块。
不真连市场与公网。
"""
import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import httpx
import pytest

import mcps.manager as manager
import mcps.platform as platform

# ===== 1. 配置解析 =====

def test_config_missing_means_disabled():
    cfg = platform.PlatformMCPConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.base_url == "" and cfg.service_token == ""
    assert cfg.refresh_seconds == 300 and cfg.timeout == 60


def test_config_both_present_strips_and_applies():
    cfg = platform.PlatformMCPConfig.from_env({
        "MARKET_BASE_URL": " https://market.example.com/ ",
        "MARKET_SERVICE_TOKEN": " tok ",
    })
    assert cfg.enabled is True
    assert cfg.base_url == "https://market.example.com"
    assert cfg.service_token == "tok"


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "True"])
def test_config_invalid_numbers_fall_back(bad):
    cfg = platform.PlatformMCPConfig.from_env({
        "MARKET_BASE_URL": "http://m", "MARKET_SERVICE_TOKEN": "t",
        "MARKET_PLATFORM_REFRESH_SECONDS": bad, "MARKET_PLATFORM_TIMEOUT": bad,
    })
    assert cfg.refresh_seconds == 300 and cfg.timeout == 60


def test_config_custom_numbers():
    cfg = platform.PlatformMCPConfig.from_env({
        "MARKET_BASE_URL": "http://m", "MARKET_SERVICE_TOKEN": "t",
        "MARKET_PLATFORM_REFRESH_SECONDS": "45", "MARKET_PLATFORM_TIMEOUT": "12.5",
    })
    assert cfg.refresh_seconds == 45 and cfg.timeout == 12.5


@pytest.mark.parametrize("env", [{"MARKET_BASE_URL": "http://m"}, {"MARKET_SERVICE_TOKEN": "t"}])
def test_config_partial_is_disabled(env):
    assert platform.PlatformMCPConfig.from_env(env).enabled is False


# ===== 2. sync 解析 =====

def test_parse_sync_filters_type_and_distribution():
    payload = [
        {"name": "remote-one", "type": "mcp", "version": "1.0.0",
         "distribution": "remote", "gateway": None},
        {"name": "both-one", "type": "mcp", "version": "2.0.0", "distribution": "both",
         "gateway": {"stream_url": "/api/mcp-gateway/relay/both-one/stream", "transport": "stream"}},
        {"name": "local-one", "type": "mcp", "version": "1.0.0", "distribution": "local", "gateway": None},
        {"name": "skill-one", "type": "skill", "version": "1.0.0", "distribution": "remote"},
        {"name": "no-dist", "type": "mcp", "version": "1.0.0"},
        "not-a-dict",
        {"type": "mcp", "distribution": "remote"},
    ]
    caps = platform.parse_sync_capabilities(payload)
    assert [c["name"] for c in caps] == ["remote-one", "both-one", "no-dist"]
    assert caps[1]["gateway"]["stream_url"].endswith("/stream")
    assert caps[2]["distribution"] == "both"  # 缺省与市场产出端一致
    assert caps[0]["status"] == ""  # 缺字段兜底空串


def test_parse_sync_non_list_returns_empty():
    assert platform.parse_sync_capabilities({"detail": "x"}) == []
    assert platform.parse_sync_capabilities(None) == []


# ===== 3. 暴露名与 relay URL =====

def test_exposed_tool_name_prefix_and_sanitize():
    assert platform.exposed_tool_name("dingtalk", "send_message") == "platform__dingtalk__send_message"
    weird = platform.exposed_tool_name("能力 A", "工.具/名")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", weird)
    assert len(platform.exposed_tool_name("x" * 80, "y" * 80)) <= manager.MCP_MAX_EXPOSED_NAME_LEN


def test_relay_stream_url_builds_default_and_uses_gateway_payload():
    assert platform.relay_stream_url("http://m", "cap") == "http://m/api/mcp-gateway/relay/cap/stream"
    assert (platform.relay_stream_url("http://m/", "cap", version="1.2.0")
            == "http://m/api/mcp-gateway/relay/cap@1.2.0/stream")
    assert (platform.relay_stream_url("http://m", "cap", version="not-semver")
            == "http://m/api/mcp-gateway/relay/cap/stream")  # 非 semver 不钉(网关按整名解析)
    gw = {"stream_url": "/api/mcp-gateway/relay/caf%C3%A9/stream"}
    assert (platform.relay_stream_url("http://m", "café", gateway=gw)
            == "http://m/api/mcp-gateway/relay/caf%C3%A9/stream")
    assert platform.relay_stream_url("http://m", "a/b") == "http://m/api/mcp-gateway/relay/a%2Fb/stream"


# ===== 4. PlatformMCPClient 集成(stub HTTP + 假 session) =====

def _cap(name, version="1.0.0", distribution="remote", gateway=None):
    return {"name": name, "type": "mcp", "version": version, "status": "published",
            "distribution": distribution, "gateway": gateway}


def _tool(name, *, read_only=None, destructive=None):
    annotations = None
    if read_only is not None or destructive is not None:
        annotations = SimpleNamespace(read_only_hint=read_only, destructive_hint=destructive)
    return SimpleNamespace(name=name, description=f"d-{name}",
                           input_schema={"type": "object"}, annotations=annotations)


class _FakeSession:
    def __init__(self, tools, calls, *, fail_call=None, delay=0.0):
        self._tools, self.calls = tools, calls
        self._fail_call, self._delay = fail_call, delay

    async def list_tools(self):
        return SimpleNamespace(tools=self._tools)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail_call is not None:
            raise self._fail_call
        return SimpleNamespace(content=[SimpleNamespace(text=f"ok:{name}")])


class _SyncStub:
    def __init__(self, caps):
        self.caps = caps
        self.requests = []
        self.fail = False

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self.fail:
                return httpx.Response(500, json={"detail": "market down"})
            return httpx.Response(200, json=self.caps)
        return httpx.MockTransport(handler)


def _session_opener(sessions, entered, exited, failing=frozenset()):
    @asynccontextmanager
    async def opener(url, headers):
        # URL 形如 .../relay/{name}[@version]/stream; 版本钉定不影响能力名索引
        name = url.split("/relay/", 1)[1].rsplit("/", 1)[0].split("@", 1)[0]
        if name in failing:
            raise RuntimeError("connect refused")
        entered.append((name, headers))
        try:
            yield sessions[name]
        finally:
            exited.append(name)
    return opener


def _make_client(caps, sessions, entered, exited, failing=frozenset(), timeout=60.0):
    stub = _SyncStub(caps)
    client = platform.PlatformMCPClient(
        platform.PlatformMCPConfig(base_url="http://market.test", service_token="svc-token",
                                   refresh_seconds=300, timeout=timeout),
        transport=stub.transport(),
        session_opener=_session_opener(sessions, entered, exited, failing),
    )
    return client, stub


async def test_refresh_connects_registers_tools_and_routes_call():
    calls = []
    sessions = {"cap-a": _FakeSession([
        _tool("read_t", read_only=True),
        _tool("write_t", read_only=False, destructive=False),
        _tool("dest_t", read_only=False, destructive=True),
        _tool("no_anno"),
    ], calls)}
    entered, exited = [], []
    client, stub = _make_client([_cap("cap-a")], sessions, entered, exited)
    try:
        assert await client.refresh_once() is True
        assert str(stub.requests[0].url) == "http://market.test/api/capabilities/sync"
        assert stub.requests[0].headers["authorization"] == "Bearer svc-token"
        assert entered and entered[0][0] == "cap-a"
        assert entered[0][1]["Authorization"] == "Bearer svc-token"

        names = [d["function"]["name"] for d in client.tool_defs]
        assert names == ["platform__cap-a__read_t", "platform__cap-a__write_t",
                         "platform__cap-a__dest_t", "platform__cap-a__no_anno"]
        assert client.has_tool("platform__cap-a__read_t")
        assert client.tool_risk("platform__cap-a__read_t") == "read"
        assert client.tool_risk("platform__cap-a__write_t") == "write"
        assert client.tool_risk("platform__cap-a__dest_t") == "destructive"
        assert client.tool_risk("platform__cap-a__no_anno") == "unknown"
        assert client.tool_risk("platform__cap-a__missing") is None
        assert client.tool_server("platform__cap-a__read_t") == "platform:cap-a"
        assert client.tool_raw("platform__cap-a__read_t") == "read_t"

        assert await client.call_tool("platform__cap-a__read_t", {"q": 1}) == "ok:read_t"
        assert calls[-1] == ("read_t", {"q": 1})
        assert await client.call_tool("platform__cap-a__missing", {}) == \
            "工具 platform__cap-a__missing 未找到"

        status = client.status()
        row = status["servers"][0]
        assert row["name"] == "cap-a" and row["version"] == "1.0.0"
        assert row["connected"] is True and row["tools"] == 4
        assert row["last_error"] == "" and row["last_refresh"]
        assert row["source"] == "platform"
        assert status["summary"] == {"connected": 1, "failed": 0, "total": 1}
    finally:
        await client.close()
    assert exited == ["cap-a"]
    assert client.tool_defs == [] and not client.has_tool("platform__cap-a__read_t")


async def test_refresh_isolates_single_capability_failure():
    calls = []
    sessions = {"cap-ok": _FakeSession([_tool("t", read_only=True)], calls)}
    entered, exited = [], []
    failing = {"cap-bad"}
    client, _ = _make_client([_cap("cap-bad"), _cap("cap-ok")], sessions, entered, exited,
                             failing=failing)
    try:
        assert await client.refresh_once() is True  # sync 成功, 单个能力失败不抛
        assert client.has_tool("platform__cap-ok__t")
        assert not client.has_tool("platform__cap-bad__t")
        rows = {r["name"]: r for r in client.status()["servers"]}
        assert rows["cap-ok"]["connected"] is True
        assert rows["cap-bad"]["connected"] is False
        assert "connect refused" in rows["cap-bad"]["last_error"]
        assert client.status()["summary"] == {"connected": 1, "failed": 1, "total": 2}

        failing.discard("cap-bad")  # 下轮刷新重试并成功
        sessions["cap-bad"] = _FakeSession([_tool("t2", read_only=True)], calls)
        assert await client.refresh_once() is True
        assert client.has_tool("platform__cap-bad__t2")
        assert client.status()["summary"] == {"connected": 2, "failed": 0, "total": 2}
    finally:
        await client.close()


async def test_start_runs_refresh_in_background_and_close_cancels():
    calls = []
    sessions = {"cap-a": _FakeSession([_tool("t", read_only=True)], calls)}
    entered, exited = [], []
    client, _ = _make_client([_cap("cap-a")], sessions, entered, exited)
    client.start()
    try:
        for _ in range(100):  # 等首轮后台刷新完成(最多 ~2s)
            if client.has_tool("platform__cap-a__t"):
                break
            await asyncio.sleep(0.02)
        assert client.has_tool("platform__cap-a__t")
    finally:
        await client.close()
    assert client._refresh_task is None
    assert client.tool_defs == [] and not client.has_tool("platform__cap-a__t")


async def test_sync_failure_keeps_existing_tools_and_retries_next_refresh():
    calls = []
    sessions = {"cap-a": _FakeSession([_tool("t", read_only=True)], calls)}
    entered, exited = [], []
    client, stub = _make_client([_cap("cap-a")], sessions, entered, exited)
    try:
        assert await client.refresh_once() is True
        stub.fail = True
        assert await client.refresh_once() is False
        assert client.has_tool("platform__cap-a__t")  # 既有连接与工具保留
        assert "目录同步失败" in client.status()["last_error"]
        stub.fail = False
        assert await client.refresh_once() is True
        assert client.status()["last_error"] == ""
    finally:
        await client.close()


async def test_refresh_removes_stale_capability_and_version_change_reconnects():
    calls = []
    sessions = {"cap-a": _FakeSession([_tool("t", read_only=True)], calls)}
    entered, exited = [], []
    client, stub = _make_client([_cap("cap-a")], sessions, entered, exited)
    try:
        assert await client.refresh_once() is True
        stub.caps = [_cap("cap-a", version="2.0.0")]
        assert await client.refresh_once() is True
        assert entered == [("cap-a", {"Authorization": "Bearer svc-token"})] * 2
        assert exited == ["cap-a"]  # 版本变化 → 旧会话关闭重连
        assert client.status()["servers"][0]["version"] == "2.0.0"

        stub.caps = []
        assert await client.refresh_once() is True
        assert not client.has_tool("platform__cap-a__t")
        assert client.tool_defs == []
        assert client.status()["summary"] == {"connected": 0, "failed": 0, "total": 0}
    finally:
        await client.close()


async def test_call_tool_failure_marks_error_and_disconnect_is_isolated():
    calls = []
    sessions = {"cap-a": _FakeSession([_tool("boom_t"), _tool("closed_t")], calls,
                                      fail_call=ConnectionError("stream closed"))}
    entered, exited = [], []
    client, _ = _make_client([_cap("cap-a")], sessions, entered, exited)
    try:
        await client.refresh_once()
        out = await client.call_tool("platform__cap-a__boom_t", {})
        assert out.startswith("执行失败") and "stream closed" in out
        assert "stream closed" in client.status()["servers"][0]["last_error"]
        out2 = await client.call_tool("platform__cap-a__closed_t", {})
        assert out2 == "MCP [platform:cap-a] 未连接"
    finally:
        await client.close()


async def test_call_tool_timeout_uses_configured_timeout():
    calls = []
    sessions = {"cap-a": _FakeSession([_tool("slow_t")], calls, delay=1.0)}
    entered, exited = [], []
    client, _ = _make_client([_cap("cap-a")], sessions, entered, exited, timeout=0.05)
    try:
        await client.refresh_once()
        out = await client.call_tool("platform__cap-a__slow_t", {})
        assert out == "执行失败: 工具调用超时"
        assert "工具调用超时" in client.status()["servers"][0]["last_error"]
    finally:
        await client.close()


# ===== 5. 与 MCPManager 合并 =====

class _FakePlatform:
    """duck 假平台: 与 PlatformMCPClient 相同接口, 便于 manager 合并测试。"""

    def __init__(self, risk="read"):
        self.risk = risk
        self.calls: list = []
        self.closed = False
        self.managers: list = []
        self.tool_defs = [{"type": "function", "function": {
            "name": "platform__cap__t", "description": "平台工具", "parameters": {"type": "object"}}}]

    def attach_manager(self, mgr):
        self.managers.append(mgr)

    def has_tool(self, name):
        return any(d["function"]["name"] == name for d in self.tool_defs)

    def tool_risk(self, name):
        return self.risk if self.has_tool(name) else None

    def tool_server(self, name):
        return "platform:cap" if self.has_tool(name) else None

    def tool_raw(self, name):
        return "t" if self.has_tool(name) else None

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return "pong"

    def status(self):
        connected = bool(self.tool_defs)
        return {
            "enabled": True,
            "base_url": "http://market.test",
            "last_refresh": "2026-09-24T00:00:00",
            "last_error": "",
            "servers": [
                {"name": "cap", "version": "1.0.0", "connected": connected, "tools": 1 if connected else 0,
                 "last_error": "", "last_refresh": "2026-09-24T00:00:00", "source": "platform"},
            ],
            "summary": {"connected": 1 if connected else 0, "failed": 0 if connected else 1, "total": 1},
        }

    async def close(self):
        self.closed = True


def _local_manager_with_tool(exposed: str):
    """构造带一个本地工具的 manager(绕过 stdio 连接)。"""
    async def _noop_close():
        return None

    mgr = manager.MCPManager("")
    mgr.servers["local_srv"] = SimpleNamespace(
        tool_defs=[{"type": "function", "function": {
            "name": exposed, "description": "本地工具", "parameters": {"type": "object"}}}],
        risk_of=lambda _n: "read",
        close=_noop_close,
    )
    mgr._rebuild_tool_defs()
    return mgr


async def test_manager_merges_platform_tools_risk_and_dispatch():
    mgr = manager.MCPManager("")
    fake = _FakePlatform()
    mgr.attach_platform(fake)
    try:
        assert fake.managers == [mgr]
        assert [d["function"]["name"] for d in mgr.tool_defs] == ["platform__cap__t"]
        assert mgr.has_tool("platform__cap__t")
        assert mgr.tool_risk("platform__cap__t") == "read"
        assert mgr.tool_server("platform__cap__t") == "platform:cap"
        assert mgr.tool_raw("platform__cap__t") == "t"
        assert mgr.tool_risk("shell") is None
        assert await mgr.call_tool("platform__cap__t", {"x": 1}) == "pong"
        assert fake.calls == [("platform__cap__t", {"x": 1})]
        assert await mgr.call_tool("nope", {}) == "工具 nope 未找到"

        # 平台工具刷新后: 通知已挂接 manager 重建工具表(生产由 PlatformMCPClient 触发)
        fake.tool_defs = []
        for m in fake.managers:
            m._rebuild_tool_defs()
        assert mgr.tool_defs == [] and not mgr.has_tool("platform__cap__t")
    finally:
        await mgr.close()
    assert fake.closed


async def test_manager_local_tool_wins_on_name_collision():
    mgr = _local_manager_with_tool("echo")
    fake = _FakePlatform()
    fake.tool_defs = [{"type": "function", "function": {
        "name": "echo", "description": "平台", "parameters": {"type": "object"}}}]
    mgr.attach_platform(fake)
    try:
        assert mgr.tool_server("echo") == "local_srv"  # 本地优先, 平台同名被跳过
        names = [d["function"]["name"] for d in mgr.tool_defs]
        assert names.count("echo") == 1
    finally:
        await mgr.close()


async def test_status_all_includes_platform_track():
    mgr = manager.MCPManager("")
    fake = _FakePlatform()
    mgr.attach_platform(fake)
    try:
        data = manager.MCPManager.status_all()
        assert data["platform"] is not None
        rows = data["platform"]["servers"]
        assert rows and rows[0]["name"] == "cap" and rows[0]["source"] == "platform"
        assert data["platform"]["summary"] == {"connected": 1, "failed": 0, "total": 1}
    finally:
        await mgr.close()
    assert manager.MCPManager.status_all()["platform"] is None


# ===== 6. executor 审计归属(平台 server 名) =====

class _FakeStorage:
    def __init__(self):
        self.calls = []

    def record_mcp_call(self, **kwargs):
        self.calls.append(kwargs)
        return len(self.calls)


async def test_execute_tool_routes_platform_and_audits():
    from agent.core import RunContext, _current_run
    from agent.executor import execute_tool

    mgr = manager.MCPManager("")
    mgr.attach_platform(_FakePlatform())
    agent = SimpleNamespace(mcp=mgr, tool_registry=None, skill_manager=None,
                            plugin_manager=None, storage=_FakeStorage())
    rc = RunContext(user_id="web:7", conversation_id="web:7:abc")
    tok = _current_run.set(rc)
    try:
        out = await execute_tool(agent, "platform__cap__t", {"x": 1})
    finally:
        _current_run.reset(tok)
        await mgr.close()
    assert out == "pong"
    rec = agent.storage.calls[0]
    assert rec["server"] == "platform:cap" and rec["tool"] == "t"
    assert rec["exposed"] == "platform__cap__t" and rec["channel"] == "web"


async def test_destructive_platform_tool_marks_run_sensitive():
    from agent.core import RunContext, _current_run
    from agent.executor import _mark_run_sensitive_if_hit

    mgr = manager.MCPManager("")
    mgr.attach_platform(_FakePlatform(risk="destructive"))
    agent = SimpleNamespace(mcp=mgr)
    rc = RunContext(user_id="web:7", run_id="rid-platform")
    tok = _current_run.set(rc)
    try:
        _mark_run_sensitive_if_hit(agent, "platform__cap__t")
    finally:
        _current_run.reset(tok)
        await mgr.close()
    assert rc.sensitive_hit is True


# ===== 7. Agent 平台轨挂接门控(root/worker 开放, 子代理关闭) =====

def _bare_agent(*, parent=None, platform_enabled=True, configs=()):
    from agent.core import Agent

    agent = Agent.__new__(Agent)
    agent.parent_agent = parent
    agent.platform_mcp_enabled = platform_enabled
    agent._subagent_mcp_configs = list(configs)
    agent._read_mcp_config_file = lambda: []
    calls = []

    async def fake_connect(subagent=False, platform_config=None):
        calls.append((subagent, platform_config))

    agent._connect_mcp_servers = fake_connect
    return agent, calls


async def test_load_mcp_servers_platform_gating(monkeypatch):
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.test")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "tok")

    root, root_calls = _bare_agent()
    await root._load_mcp_servers()
    assert len(root_calls) == 1 and root_calls[0][0] is False
    assert root_calls[0][1].enabled is True

    worker, worker_calls = _bare_agent(parent=object())
    await worker._load_mcp_servers()
    assert len(worker_calls) == 1 and worker_calls[0][0] is False  # worker 也挂平台轨
    assert worker_calls[0][1].enabled is True

    subagent, sub_calls = _bare_agent(parent=object(), platform_enabled=False)
    await subagent._load_mcp_servers()
    assert sub_calls == []  # 子代理无本地配置时不建 manager, 也不挂平台


async def test_load_mcp_servers_platform_disabled(monkeypatch):
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)

    root, root_calls = _bare_agent()
    await root._load_mcp_servers()
    assert root_calls == []

    worker, worker_calls = _bare_agent(parent=object())
    await worker._load_mcp_servers()
    assert worker_calls == []


async def test_load_mcp_servers_local_configs_still_connect(monkeypatch):
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)

    subagent, calls = _bare_agent(parent=object(), platform_enabled=False, configs=[{"name": "s"}])
    subagent._read_mcp_config_file = lambda: [{"name": "s"}]
    await subagent._load_mcp_servers()
    assert len(calls) == 1 and calls[0][0] is True
    assert calls[0][1].enabled is False  # 子代理不启用平台轨


# ===== 8. /healthz 平台汇总 =====
def test_healthz_includes_platform_summary(monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "1")

    def fake_status_all():
        return {
            "servers": [{"name": "w2_time", "enabled": True, "connected": True, "tools": 3,
                         "last_error": "", "reconnects": 0, "timeout_seconds": 60.0,
                         "max_concurrency": 4, "source": "local"}],
            "summary": {"connected": 1, "failed": 0, "total": 1},
            "platform": {
                "enabled": True, "base_url": "http://market.test",
                "servers": [{"name": "cap-a", "version": "1.0.0", "connected": False, "tools": 0,
                             "last_error": "连接失败: RuntimeError: boom",
                             "last_refresh": "", "source": "platform"}],
                "summary": {"connected": 0, "failed": 1, "total": 1},
            },
        }

    monkeypatch.setattr(manager.MCPManager, "status_all", staticmethod(fake_status_all))
    from fastapi.testclient import TestClient

    from web.server import WebServer
    client = TestClient(WebServer()._app)
    body = client.get("/healthz").json()
    assert body["mcp"]["platform"]["summary"] == {"connected": 0, "failed": 1, "total": 1}
    assert body["mcp"]["platform"]["failed"] == ["cap-a"]
