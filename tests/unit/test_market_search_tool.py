"""市场目录检索工具(market 身份)单测。

覆盖:
- 逐请求携带 market 受众用户 token(Bearer, aud=market)并走 GET /api/capabilities/task-search;
- 命中分组(agents/skills/mcps/tools)与 total;
- 真实用户无 token 时 fail-closed；仅无数字 uid 的系统 run 用服务令牌;
- joined 标记: 已开通优先 + 未开通 note 提示(方案A: 发现保留, 不误调未开通能力)。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.core import RunContext, _current_run  # noqa: E402
from tools import market_search as ms  # noqa: E402
from tools.market_search import MarketSearchTool  # noqa: E402
from web.sso_tokens import USER_TOKEN_HINT as _HINT  # noqa: E402

_DEFAULT_PAYLOAD = {
    "q": "报销",
    "terms": ["报销"],
    "agents": [{"name": "财务专家", "type": "agent", "description": "报销/预算", "match_score": 12.0}],
    "skills": [{"name": "reimburse", "type": "skill", "description": "报销流程"}],
    "mcps": [],
    "plugins": [],
    "others": [],
}


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    captured: dict = {}
    payload: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None, **kwargs):
        _FakeClient.captured = {"url": url, "headers": headers, "params": params}
        return _FakeResp(200, _FakeClient.payload or _DEFAULT_PAYLOAD)


@pytest.fixture(autouse=True)
def _market_env(monkeypatch):
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.local")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "svc-token")
    _FakeClient.captured = {}
    _FakeClient.payload = {}
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


def test_market_search_tool_schema_is_object():
    tool = MarketSearchTool()
    params = tool.parameters
    assert params.get("type") == "object"
    assert isinstance(params.get("properties"), dict)
    assert "query" in params["properties"]
    assert params.get("required") == ["query"]


async def _run_with(user_id: str, coro_factory, role: str = "default"):
    rc = RunContext(user_id=user_id, role=role)
    token = _current_run.set(rc)
    try:
        return await coro_factory()
    finally:
        _current_run.reset(token)


async def test_market_search_tool_sends_user_token_and_groups(monkeypatch, hosted_token):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    tool = MarketSearchTool()
    out = await _run_with("web:7", lambda: tool.execute(query="报销", limit=5))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["total"] == 2
    assert payload["agents"][0]["name"] == "财务专家"
    assert payload["skills"][0]["name"] == "reimburse"

    cap = _FakeClient.captured
    assert cap["url"] == "http://market.local/api/capabilities/task-search"
    assert cap["headers"]["Authorization"] == "Bearer user-tok-7"
    assert "X-Act-As-Sub" not in cap["headers"]
    assert cap["params"] == {"q": "报销"}
    assert _TOKEN_CALLS[-1] == (7, "market")


async def test_market_search_tool_admin_uses_user_token(monkeypatch, hosted_token):
    """管理员也是真实用户：按自己的 market 身份检索。"""
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    tool = MarketSearchTool()
    out = await _run_with("web:7", lambda: tool.execute(query="报销"), role="admin")
    payload = json.loads(out)
    assert payload["ok"] is True
    cap = _FakeClient.captured
    assert cap["headers"]["Authorization"] == "Bearer user-tok-7"
    assert _TOKEN_CALLS[-1] == (7, "market")


async def test_market_search_tool_real_user_without_token_fails_closed(monkeypatch):
    """真实用户（即使 role=admin）无 token：fail-closed，不用服务令牌。"""
    from web import sso_tokens

    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: "")
    tool = MarketSearchTool()
    out = await _run_with("web:7", lambda: tool.execute(query="报销"), role="admin")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_market_search_tool_system_without_token_uses_service_token(monkeypatch):
    """无数字 uid 的系统 run：允许服务令牌（平台身份）检索。"""
    from web import sso_tokens

    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: "")
    tool = MarketSearchTool()
    out = await _run_with("scheduler:admin", lambda: tool.execute(query="报销"), role="admin")
    payload = json.loads(out)
    assert payload["ok"] is True
    cap = _FakeClient.captured
    assert cap["headers"]["Authorization"] == "Bearer svc-token"


async def test_market_search_tool_kind_filter(monkeypatch, hosted_token):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    tool = MarketSearchTool()
    out = await _run_with("web:7", lambda: tool.execute(query="报销", kind="agent"))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["kind"] == "agent"
    assert payload["total"] == 1
    assert payload["agents"][0]["name"] == "财务专家"
    assert "skills" not in payload


async def test_market_search_tool_requires_numeric_uid(monkeypatch):
    """uid 解析失败(空/中文旧账号名) → fail-closed 统一引导登录, 不崩溃、不发请求。"""
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    tool = MarketSearchTool()
    for user_id in ("", "web:朱尚荣", "web:0"):
        out = await _run_with(user_id, lambda: tool.execute(query="报销"))
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_market_search_tool_requires_hosted_token(monkeypatch):
    from web import sso_tokens

    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: "")
    tool = MarketSearchTool()
    out = await _run_with("web:7", lambda: tool.execute(query="报销"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_market_search_tool_disabled_without_config(monkeypatch, hosted_token):
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    tool = MarketSearchTool()
    out = await _run_with("web:7", lambda: tool.execute(query="报销"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "未配置" in payload["error"]
    assert _FakeClient.captured == {}


# ---- joined 标记: 已开通优先 + 未开通 note(方案A) ----

async def test_market_search_marks_joined_and_notes_locked(monkeypatch, hosted_token):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    _FakeClient.payload = {
        "q": "发货", "terms": ["发货"],
        "agents": [{"name": "数字中台", "type": "agent", "joined": False}],
        "skills": [{"name": "report-writer", "type": "skill", "joined": True}],
        "mcps": [], "others": [], "plugins": [],
    }
    tool = MarketSearchTool()
    out = await _run_with("web:7", lambda: tool.execute(query="发货"))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert _FakeClient.captured["url"] == "http://market.local/api/capabilities/task-search"
    assert _FakeClient.captured["headers"]["Authorization"] == "Bearer user-tok-7"
    assert payload["agents"][0]["joined"] is False
    assert payload["skills"][0]["joined"] is True
    assert "未开通" in payload["note"] and "数字中台" in payload["note"]


async def test_market_search_joined_first_sort(monkeypatch, hosted_token):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    _FakeClient.payload = {
        "q": "x", "terms": [],
        "agents": [], "mcps": [], "others": [], "plugins": [],
        "skills": [
            {"name": "a", "joined": False},
            {"name": "b", "joined": True},
        ],
    }
    out = json.loads(await _run_with(
        "web:7", lambda: MarketSearchTool().execute(query="x", kind="skill")))
    assert [i["name"] for i in out["skills"]] == ["b", "a"]
    assert "note" in out


async def test_market_search_no_note_when_all_joined(monkeypatch, hosted_token):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    _FakeClient.payload = {
        "q": "x", "terms": [],
        "agents": [{"name": "数字中台", "joined": True}],
        "skills": [], "mcps": [], "others": [], "plugins": [],
    }
    out = json.loads(await _run_with(
        "web:7", lambda: MarketSearchTool().execute(query="x")))
    assert "note" not in out
