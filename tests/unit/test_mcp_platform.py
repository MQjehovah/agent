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
import builtins
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


def test_parse_sync_prefers_runtime_cloud_over_distribution():
    payload = [
        # runtime.cloud=False（即使 distribution=both）→ 跳过
        {"name": "cloud-off", "type": "mcp", "distribution": "both",
         "runtime": {"cloud": False, "local": True, "recommended": "local"}},
        # runtime.cloud=True 优先接受
        {"name": "cloud-on", "type": "mcp", "distribution": "remote",
         "runtime": {"cloud": True, "local": True, "recommended": "cloud"}},
        # runtime 非法/缺失 → 回退 distribution 旧口径
        {"name": "fallback-remote", "type": "mcp", "distribution": "remote",
         "runtime": {"cloud": "yes"}},
        {"name": "fallback-no-runtime", "type": "mcp", "distribution": "remote"},
        {"name": "fallback-local", "type": "mcp", "distribution": "local"},
    ]
    caps = platform.parse_sync_capabilities(payload)
    assert [c["name"] for c in caps] == ["cloud-on", "fallback-remote", "fallback-no-runtime"]


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


# ===== 3b. 异常解包 / 错误文案 / 退避(纯函数) =====

class _FakeGroup(BaseException):
    """鸭子类型异常组(仅暴露 exceptions 属性, 不依赖 3.11+ ExceptionGroup)。"""

    def __init__(self, *children):
        super().__init__(f"{len(children)} sub-exceptions")
        self.exceptions = list(children)


def test_unwrap_error_single_exception_passthrough():
    err = ValueError("boom")
    assert platform.unwrap_error(err) is err


def test_unwrap_error_nested_and_deep_groups():
    inner = KeyError("real cause")
    nested = _FakeGroup(_FakeGroup(inner, RuntimeError("other")), ValueError("sibling"))
    assert platform.unwrap_error(nested) is inner
    deep = inner
    for _ in range(5):
        deep = _FakeGroup(deep)
    assert platform.unwrap_error(deep) is inner


def test_unwrap_error_empty_group_and_non_exception_child():
    empty = _FakeGroup()
    assert platform.unwrap_error(empty) is empty
    broken = _FakeGroup("not-an-exception")
    assert platform.unwrap_error(broken) is broken


def test_unwrap_error_real_exception_group():
    group_cls = getattr(builtins, "ExceptionGroup", None)  # 3.11+ 才有, 旧解释器跳过
    if group_cls is None:
        pytest.skip("ExceptionGroup 需 Python 3.11+")
    root = TimeoutError("real timeout")
    group = group_cls("tg", [group_cls("inner", [root])])
    assert platform.unwrap_error(group) is root


def test_format_market_error_unwraps_and_truncates_message():
    group = _FakeGroup(_FakeGroup(RuntimeError("real cause")), ValueError("other"))
    assert platform.format_market_error("连接失败", group) == "连接失败: RuntimeError: real cause"

    text = platform.format_market_error("工具调用失败: t", RuntimeError("x" * 500))
    assert text.startswith("工具调用失败: t: RuntimeError: " + "x" * 10)
    assert len(text) <= platform.MARKET_LAST_ERROR_MAX_LEN


def test_next_backoff_sequence_capped_by_refresh_period():
    seq, current = [], 0.0
    for _ in range(6):
        current = platform.next_backoff(current, 300.0)
        seq.append(current)
    assert seq == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]

    assert platform.next_backoff(0, 10.0) == 10.0  # 刷新周期小于基准时直接封顶
    assert platform.next_backoff(10.0, 10.0) == 10.0
    assert platform.next_backoff(0, 0) == 0.0


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


def _session_opener(sessions, entered, exited, failing=frozenset(), *,
                    attempts=None, active=None):
    @asynccontextmanager
    async def opener(url, headers):
        # URL 形如 .../relay/{name}[@version]/stream; 版本钉定不影响能力名索引
        name = url.split("/relay/", 1)[1].rsplit("/", 1)[0].split("@", 1)[0]
        if attempts is not None:
            attempts.append(name)
        if name in failing:
            raise RuntimeError("connect refused")
        if active is not None:
            active[0] += 1
            active[1] = max(active[1], active[0])
            await asyncio.sleep(0)  # 让同批连接进入后可观测并发
            active[0] -= 1
        entered.append((name, headers))
        try:
            yield sessions[name]
        finally:
            exited.append(name)
    return opener


def _make_client(caps, sessions, entered, exited, failing=frozenset(), timeout=60.0, *,
                 refresh_seconds=300.0, now=None, sleeper=None, attempts=None, active=None,
                 act_as="", install_filter=None):
    stub = _SyncStub(caps)
    client = platform.PlatformMCPClient(
        platform.PlatformMCPConfig(base_url="http://market.test", service_token="svc-token",
                                   refresh_seconds=refresh_seconds, timeout=timeout),
        act_as=act_as,
        transport=stub.transport(),
        session_opener=_session_opener(sessions, entered, exited, failing,
                                       attempts=attempts, active=active),
        now=now,
        sleeper=sleeper,
        install_filter=install_filter,
    )
    return client, stub


class _FakeClock:
    """可推进假时钟: 测试不真实 sleep, 只推进时钟让退避到期。"""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []
        self._waiters: list[tuple[float, asyncio.Future]] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        delay = max(0.0, float(delay))
        self.sleeps.append(delay)
        if delay <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        self._waiters.append((self.now + delay, fut))
        await fut

    def advance(self, delta: float) -> None:
        self.now += float(delta)
        keep = []
        for deadline, fut in self._waiters:
            if deadline <= self.now:
                if not fut.done():
                    fut.set_result(None)
            else:
                keep.append((deadline, fut))
        self._waiters = keep


async def _drain(rounds: int = 40) -> None:
    """让挂起任务跑起来(不推进假时钟, 不真实等待)。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def test_auth_headers_without_act_as_has_only_bearer():
    cfg = platform.PlatformMCPConfig(base_url="http://m", service_token="tok")
    client = platform.PlatformMCPClient(cfg)
    assert client._auth_headers() == {"Authorization": "Bearer tok"}


def test_auth_headers_with_act_as_appends_header():
    """act_as 非空时每个请求追加 X-Act-As-Sub(市场侧服务令牌代表用户视角)。"""
    cfg = platform.PlatformMCPConfig(base_url="http://m", service_token="tok")
    client = platform.PlatformMCPClient(cfg, act_as=" 202202100024 ")
    assert client._auth_headers() == {
        "Authorization": "Bearer tok", "X-Act-As-Sub": "202202100024"}
    # 空白 act_as 等同未设置(不追加空头)
    blank = platform.PlatformMCPClient(cfg, act_as="   ")
    assert blank._auth_headers() == {"Authorization": "Bearer tok"}


async def test_act_as_header_flows_to_sync_and_relay_session():
    """act-as 头同时作用于目录 sync 与 relay 建会话(同一 _auth_headers)。"""
    calls = []
    sessions = {"cap-a": _FakeSession([_tool("t", read_only=True)], calls)}
    entered, exited = [], []
    client, stub = _make_client([_cap("cap-a")], sessions, entered, exited,
                                act_as="202202100024")
    try:
        assert await client.refresh_once() is True
        assert stub.requests[0].headers["x-act-as-sub"] == "202202100024"
        assert entered[0][1]["X-Act-As-Sub"] == "202202100024"
        assert entered[0][1]["Authorization"] == "Bearer svc-token"
    finally:
        await client.close()


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


async def test_connect_runs_in_batches_of_two_and_isolates_failure():
    calls = []
    names = [f"cap-{i}" for i in range(5)]
    sessions = {n: _FakeSession([_tool("t", read_only=True)], calls) for n in names}
    entered, exited, attempts, active = [], [], [], [0, 0]
    clock = _FakeClock()
    client, _ = _make_client([_cap(n) for n in names], sessions, entered, exited,
                             failing={"cap-2"}, now=clock.time, sleeper=clock.sleep,
                             attempts=attempts, active=active)
    try:
        task = asyncio.create_task(client.refresh_once())
        await _drain()
        assert attempts == names[:2]  # 小批量: 首波只拉起 2 个
        assert clock.sleeps == [0.3]  # 批间隔
        clock.advance(0.3)
        await _drain()
        assert attempts == names[:4]  # cap-2 失败不阻塞同批/后续批
        clock.advance(0.3)
        await _drain()
        assert await task is True
        assert attempts == names  # 失败项也尝试过
        assert active[1] <= platform.MARKET_CONNECT_BATCH_SIZE  # 批内并发 ≤2
        assert [n for n, _ in entered] == ["cap-0", "cap-1", "cap-3", "cap-4"]
        assert client.status()["summary"] == {"connected": 4, "failed": 1, "total": 5}
        rows = {r["name"]: r for r in client.status()["servers"]}
        assert "connect refused" in rows["cap-2"]["last_error"]
    finally:
        await client.close()


async def test_fast_retry_retries_only_failed_capability_and_clears_backoff():
    calls = []
    sessions = {"cap-bad": _FakeSession([_tool("u", read_only=True)], calls),
                "cap-ok": _FakeSession([_tool("t", read_only=True)], calls)}
    entered, exited, attempts = [], [], []
    failing = {"cap-bad"}
    clock = _FakeClock()
    client, _ = _make_client([_cap("cap-bad"), _cap("cap-ok")], sessions, entered, exited,
                             failing=failing, now=clock.time, sleeper=clock.sleep,
                             attempts=attempts)
    try:
        assert await client.refresh_once() is True
        rows = {r["name"]: r for r in client.status()["servers"]}
        assert rows["cap-ok"]["connected"] is True and rows["cap-bad"]["connected"] is False
        assert attempts == ["cap-bad", "cap-ok"]
        await _drain()
        assert clock.sleeps == [30.0]  # 首次失败后 30s 快速重试
        assert not client.has_tool("platform__cap-bad__u")

        failing.discard("cap-bad")
        clock.advance(30)  # 假时钟推进到退避到期
        await _drain()
        assert attempts == ["cap-bad", "cap-ok", "cap-bad"]  # 只重试失败项
        assert client.has_tool("platform__cap-bad__u")
        cap = client._caps["cap-bad"]
        assert cap.backoff == 0 and cap.retry_at == 0  # 成功清退避计数
        clock.advance(600)
        await _drain()
        assert clock.sleeps == [30.0]  # 成功后不再排重试
    finally:
        await client.close()


async def test_fast_retry_backoff_escalates_and_caps_at_refresh_period():
    calls = []
    sessions = {"cap-bad": _FakeSession([_tool("u")], calls)}
    entered, exited, attempts = [], [], []
    clock = _FakeClock()
    client, _ = _make_client([_cap("cap-bad")], sessions, entered, exited,
                             failing={"cap-bad"}, refresh_seconds=150.0,
                             now=clock.time, sleeper=clock.sleep, attempts=attempts)
    try:
        assert await client.refresh_once() is True
        assert client._caps["cap-bad"].backoff == 30.0
        await _drain()  # 等重试任务登记首次 30s 退避
        for expected in (60.0, 120.0, 150.0, 150.0):  # 30→60→120→150(封顶)→150
            clock.advance(clock.sleeps[-1])
            await _drain()
            assert client._caps["cap-bad"].backoff == expected
        assert clock.sleeps == [30.0, 60.0, 120.0, 150.0, 150.0]
        assert attempts == ["cap-bad"] * 5  # 每次到期都重试(仍失败)
    finally:
        await client.close()


async def test_fast_retry_waits_when_refresh_connect_in_flight():
    calls = []
    sessions = {"cap-bad": _FakeSession([_tool("u")], calls)}
    entered, exited, attempts = [], [], []
    failing = {"cap-bad"}
    clock = _FakeClock()
    hang_stop = asyncio.Event()
    hang = None

    async def _hang():
        await hang_stop.wait()

    client, _ = _make_client([_cap("cap-bad")], sessions, entered, exited,
                             failing=failing, now=clock.time, sleeper=clock.sleep,
                             attempts=attempts)
    try:
        assert await client.refresh_once() is True
        await _drain()
        assert clock.sleeps == [30.0]

        cap = client._caps["cap-bad"]
        hang = asyncio.create_task(_hang())
        cap._conn_task = hang  # 模拟刷新路径正在为它建连(未完成)
        clock.advance(30)
        await _drain()
        assert clock.sleeps == [30.0, platform.MARKET_CONNECT_BATCH_INTERVAL]  # 有界等待, 不空转
        assert attempts == ["cap-bad"]  # 不重复发起连接
    finally:
        cap = client._caps.get("cap-bad")
        if cap is not None:
            cap._conn_task = None
        hang_stop.set()
        if hang is not None:
            await hang
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


# ----- 4b. 用户级云端托管安装过滤(P3) -----

async def test_install_filter_limits_capabilities_and_tools():
    caps = [_cap("cap-a"), _cap("cap-b")]
    sessions = {"cap-a": _FakeSession([_tool("t-a")], []),
                "cap-b": _FakeSession([_tool("t-b")], [])}
    entered, exited = [], []
    client, _ = _make_client(caps, sessions, entered, exited,
                             install_filter=lambda: {"cap-a"})
    try:
        assert await client.refresh_once() is True
        assert sorted(client._caps) == ["cap-a"]
        assert [e[0] for e in entered] == ["cap-a"]
        names = [d["function"]["name"] for d in client.tool_defs]
        assert names == [platform.exposed_tool_name("cap-a", "t-a")]
    finally:
        await client.close()


async def test_install_filter_empty_or_error_yields_no_capabilities(caplog):
    import logging

    for flt in (lambda: set(),
                lambda: (_ for _ in ()).throw(RuntimeError("db down"))):
        caps = [_cap("cap-a")]
        sessions = {"cap-a": _FakeSession([_tool("t-a")], [])}
        entered, exited = [], []
        client, _ = _make_client(caps, sessions, entered, exited, install_filter=flt)
        try:
            with caplog.at_level(logging.WARNING, logger="agent.mcps.platform"):
                assert await client.refresh_once() is True
            assert client._caps == {}
            assert client.tool_defs == []
            assert entered == []                       # 未连接任何能力
        finally:
            await client.close()
    assert any("安装过滤读取失败" in r.getMessage() for r in caplog.records)


async def test_install_filter_reapplied_on_refresh():
    """过滤集合变化后: 新允许的能力连上, 被移除的能力下线。"""
    caps = [_cap("cap-a"), _cap("cap-b")]
    sessions = {"cap-a": _FakeSession([_tool("t-a")], []),
                "cap-b": _FakeSession([_tool("t-b")], [])}
    entered, exited = [], []
    allowed = {"cap-a"}
    client, _ = _make_client(caps, sessions, entered, exited,
                             install_filter=lambda: set(allowed))
    try:
        assert await client.refresh_once() is True
        assert sorted(client._caps) == ["cap-a"]
        allowed = {"cap-b"}
        assert await client.refresh_once() is True
        assert sorted(client._caps) == ["cap-b"]
        names = [d["function"]["name"] for d in client.tool_defs]
        assert names == [platform.exposed_tool_name("cap-b", "t-b")]
    finally:
        await client.close()


async def test_no_install_filter_keeps_all_capabilities():
    """root(不传 filter)保持服务令牌全量视角。"""
    caps = [_cap("cap-a"), _cap("cap-b")]
    sessions = {"cap-a": _FakeSession([_tool("t-a")], []),
                "cap-b": _FakeSession([_tool("t-b")], [])}
    entered, exited = [], []
    client, _ = _make_client(caps, sessions, entered, exited)
    try:
        assert await client.refresh_once() is True
        assert sorted(client._caps) == ["cap-a", "cap-b"]
        assert len(client.tool_defs) == 2
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


async def test_connect_mcp_servers_passes_platform_act_as(monkeypatch):
    """Agent._connect_mcp_servers 把实例级 platform_act_as 透传给 PlatformMCPClient。"""
    import mcps as mcps_pkg

    captured = {}

    class _FakePlatformClient:
        def __init__(self, config, *, act_as="", install_filter=None):
            captured["config_enabled"] = config.enabled
            captured["act_as"] = act_as
            captured["install_filter"] = install_filter

        def start(self):
            captured["started"] = True

    class _FakeMCPManager:
        def __init__(self, *args, **kwargs):
            self.platform = None

        def set_reserved_names(self, names):
            captured["reserved"] = set(names)

        def attach_platform(self, client):
            self.platform = client

        def start_health_check(self):
            captured["health"] = True

    monkeypatch.setattr(mcps_pkg, "MCPManager", _FakeMCPManager)
    monkeypatch.setattr(platform, "PlatformMCPClient", _FakePlatformClient)

    from agent.core import Agent

    agent = Agent.__new__(Agent)
    agent.mcp_configs = []
    agent.platform_act_as = "202202100024"
    agent.owner_uid = 0                       # 未注入归属身份(非 worker): 不过滤
    agent.name = "test"
    agent.tool_registry = None
    agent.skill_manager = None
    agent.plugin_manager = None
    agent.tool_denylist = set()

    await agent._connect_mcp_servers(
        subagent=False,
        platform_config=platform.PlatformMCPConfig(base_url="http://m", service_token="t"),
    )
    assert captured["act_as"] == "202202100024"
    assert captured["install_filter"] is None
    assert captured["config_enabled"] is True
    assert captured["started"] is True and captured["health"] is True


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
