"""市场目录检索工具(代授权/OBO)单测。

覆盖:
- 逐请求携带 X-Act-As-Sub 并走 GET /api/capabilities/task-search;
- 命中分组(agents/skills/mcps/tools)与 total;
- 无 subject 时 fail-closed; 市场未配置时返回错误。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.core import RunContext, _current_run  # noqa: E402
from tools import market_search as ms  # noqa: E402
from tools.market_search import MarketSearchTool  # noqa: E402


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None, **kwargs):
        _FakeClient.captured = {"url": url, "headers": headers, "params": params}
        return _FakeResp(200, {
            "q": "报销",
            "terms": ["报销"],
            "agents": [{"name": "财务专家", "type": "agent", "description": "报销/预算", "match_score": 12.0}],
            "skills": [{"name": "reimburse", "type": "skill", "description": "报销流程"}],
            "mcps": [],
            "plugins": [],
            "others": [],
        })


@pytest.fixture(autouse=True)
def _market_env(monkeypatch):
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.local")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "svc-token")
    _FakeClient.captured = {}
    yield


def test_market_search_tool_schema_is_object():
    tool = MarketSearchTool()
    params = tool.parameters
    assert params.get("type") == "object"
    assert isinstance(params.get("properties"), dict)
    assert "query" in params["properties"]
    assert params.get("required") == ["query"]


async def _run_with(user_id: str, coro_factory):
    rc = RunContext(user_id=user_id, role="default")
    token = _current_run.set(rc)
    try:
        return await coro_factory()
    finally:
        _current_run.reset(token)


async def test_market_search_tool_sends_subject_and_groups(monkeypatch):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    tool = MarketSearchTool()
    out = await _run_with("web:jimingqing", lambda: tool.execute(query="报销", limit=5))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["total"] == 2
    assert payload["agents"][0]["name"] == "财务专家"
    assert payload["skills"][0]["name"] == "reimburse"

    cap = _FakeClient.captured
    assert cap["url"] == "http://market.local/api/capabilities/task-search"
    assert cap["headers"]["Authorization"] == "Bearer svc-token"
    assert cap["headers"]["X-Act-As-Sub"] == "jimingqing"
    assert cap["params"] == {"q": "报销"}


async def test_market_search_tool_kind_filter(monkeypatch):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    tool = MarketSearchTool()
    out = await _run_with("web:jimingqing", lambda: tool.execute(query="报销", kind="agent"))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["kind"] == "agent"
    assert payload["total"] == 1
    assert payload["agents"][0]["name"] == "财务专家"
    assert "skills" not in payload


async def test_market_search_tool_requires_subject(monkeypatch):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    tool = MarketSearchTool()
    out = await _run_with("", lambda: tool.execute(query="报销"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "市场身份" in payload["error"]
    assert _FakeClient.captured == {}


async def test_market_search_tool_disabled_without_config(monkeypatch):
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    tool = MarketSearchTool()
    out = await _run_with("web:jimingqing", lambda: tool.execute(query="报销"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "未配置" in payload["error"]
