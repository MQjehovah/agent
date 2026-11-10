"""市场执行工具(用户身份)单测。

覆盖:
- 逐请求携带 market 受众用户 token(Bearer, aud=market)调用 /api/runtime/*;
- tool/mcp/skill/agent 四类端点与请求体（agent 即委派专家）;
- 真实用户无托管 token 时 fail-closed；仅无数字 uid 的系统 run 用服务令牌。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.core import RunContext, _current_run  # noqa: E402
from tools import market_execute as me  # noqa: E402
from tools.market_execute import MarketExecuteTool  # noqa: E402
from web.sso_tokens import USER_TOKEN_HINT as _HINT  # noqa: E402


class _FakeResp:
    def __init__(self, status: int, payload: dict, headers: dict | None = None):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeClient:
    captured: dict = {}
    calls: list = []
    scripted: list = []  # [(status, payload, headers), ...] 第 N 次调用返回；不足回落 200

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None, **kwargs):
        entry = {"url": url, "headers": headers, "json": json}
        _FakeClient.calls.append(entry)
        _FakeClient.captured = entry
        idx = len(_FakeClient.calls) - 1
        if idx < len(_FakeClient.scripted):
            status, payload, hdrs = _FakeClient.scripted[idx]
            return _FakeResp(status, payload, hdrs)
        return _FakeResp(200, {"tool": "t", "status": "ok"})


@pytest.fixture(autouse=True)
def _market_env(monkeypatch):
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.local")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "svc-token")
    _FakeClient.captured = {}
    _FakeClient.calls = []
    _FakeClient.scripted = []
    yield


_TOKEN_CALLS: list[tuple[int, str]] = []


@pytest.fixture
def hosted_token(monkeypatch):
    """托管用户 token(按 uid 派生), 记录 audience 供断言。"""
    from web import sso_tokens

    _TOKEN_CALLS.clear()

    def _fake(uid, audience="", **kwargs):
        _TOKEN_CALLS.append((uid, audience))
        return f"user-tok-{uid}"

    monkeypatch.setattr(sso_tokens, "get_downstream_token", _fake)
    return "user-tok-7"


def test_market_execute_tool_schema_is_object():
    """回归: 工具 parameters 必须是完整 JSON Schema(type=object/properties/required)。

    曾因只返回字段字典导致 LLM 报 `type: null`。
    """
    tool = MarketExecuteTool()
    params = tool.parameters
    assert params.get("type") == "object"
    assert isinstance(params.get("properties"), dict)
    assert "capability" in params["properties"]
    assert params.get("required") == ["capability"]
    definition = tool.get_definition()
    assert definition["function"]["parameters"] == params
    assert tool.name == "market_execute"


async def _run_with(user_id: str, coro_factory, role: str = "default"):
    rc = RunContext(user_id=user_id, role=role)
    token = _current_run.set(rc)
    try:
        return await coro_factory()
    finally:
        _current_run.reset(token)


async def test_market_execute_tool_sends_user_token(monkeypatch, hosted_token):
    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    tool = MarketExecuteTool()
    out = await _run_with(
        "web:7",
        lambda: tool.execute(capability="某工具", kind="tool", tool="t", params={"a": 1}),
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["success"] is True

    cap = _FakeClient.captured
    assert cap["url"] == "http://market.local/api/runtime/tools/某工具/invoke"
    assert cap["headers"]["Authorization"] == "Bearer user-tok-7"
    assert "X-Act-As-Sub" not in cap["headers"]
    assert cap["json"] == {"tool": "t", "params": {"a": 1}}
    assert _TOKEN_CALLS[-1] == (7, "market")


async def test_market_execute_tool_admin_uses_user_token_no_retry(monkeypatch, hosted_token):
    """管理员也是真实用户：始终用自己的 market token；市场 403 不触发 agent 重试。"""
    _FakeClient.scripted = [
        (
            403,
            {"detail": "该能力按你的身份执行，缺少必需凭据：ERP_PASSWORD"},
            {"X-Market-Error-Code": "user_credentials_missing"},
        ),
    ]
    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    tool = MarketExecuteTool()
    out = await _run_with(
        "web:7",
        lambda: tool.execute(capability="某连接器", kind="mcp", tool="t", params={"x": 1}),
        role="admin",
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["status_code"] == 403
    assert len(_FakeClient.calls) == 1
    assert _FakeClient.calls[0]["headers"]["Authorization"] == "Bearer user-tok-7"


async def test_market_execute_tool_real_user_without_token_fails_closed(monkeypatch):
    """真实用户（即使 role=admin）无 token：fail-closed，不回退服务令牌。"""
    from web import sso_tokens

    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: "")
    tool = MarketExecuteTool()
    out = await _run_with(
        "web:7",
        lambda: tool.execute(capability="某连接器", kind="mcp", tool="t"),
        role="admin",
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == _HINT
    assert len(_FakeClient.calls) == 0


async def test_market_execute_tool_system_without_token_uses_service_token(monkeypatch):
    """无数字 uid 的系统 run：允许服务令牌（平台身份）执行。"""
    from web import sso_tokens

    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: "")
    tool = MarketExecuteTool()
    out = await _run_with(
        "scheduler:admin",
        lambda: tool.execute(capability="某连接器", kind="mcp", tool="t"),
        role="admin",
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert len(_FakeClient.calls) == 1
    assert _FakeClient.calls[0]["headers"]["Authorization"] == "Bearer svc-token"


async def test_market_execute_tool_agent_mode(monkeypatch, hosted_token):
    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    tool = MarketExecuteTool()
    await _run_with(
        "web:7",
        lambda: tool.execute(capability="某专家", kind="agent", task="帮我看看"),
    )
    cap = _FakeClient.captured
    assert cap["url"] == "http://market.local/api/runtime/agents/某专家/tasks"
    assert cap["json"] == {"task": "帮我看看"}


async def test_market_execute_tool_mcp_mode(monkeypatch, hosted_token):
    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    tool = MarketExecuteTool()
    await _run_with(
        "web:7",
        lambda: tool.execute(capability="某连接器", kind="mcp", tool="t", params={"x": 2}),
    )
    cap = _FakeClient.captured
    assert cap["url"] == "http://market.local/api/runtime/mcp/某连接器/call"
    assert cap["json"] == {"tool": "t", "params": {"x": 2}}


async def test_market_execute_tool_skill_mode(monkeypatch, hosted_token):
    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    tool = MarketExecuteTool()
    await _run_with(
        "web:7",
        lambda: tool.execute(capability="某技能", kind="skill", task="写周报"),
    )
    cap = _FakeClient.captured
    assert cap["url"] == "http://market.local/api/runtime/skills/某技能/activate"
    assert cap["json"] == {"context": "写周报"}


async def test_market_execute_tool_requires_numeric_uid(monkeypatch):
    """uid 解析失败(空/中文旧账号名) → fail-closed 统一引导登录, 不发请求。"""
    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    tool = MarketExecuteTool()
    for user_id in ("", "web:朱尚荣", "web:0"):
        out = await _run_with(
            user_id, lambda: tool.execute(capability="某工具", kind="tool", tool="t")
        )
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_market_execute_tool_requires_hosted_token(monkeypatch):
    from web import sso_tokens

    monkeypatch.setattr(me.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: "")
    tool = MarketExecuteTool()
    out = await _run_with(
        "web:7", lambda: tool.execute(capability="某工具", kind="tool", tool="t")
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_market_execute_tool_disabled_without_config(monkeypatch):
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    tool = MarketExecuteTool()
    out = await _run_with(
        "web:7",
        lambda: tool.execute(capability="某工具", kind="tool", tool="t"),
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "未配置" in payload["error"]
    assert _FakeClient.captured == {}
