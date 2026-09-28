"""market_search 工具(发现层): joined 标记 / 已开通优先 / 未开通提示。"""
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
    payload: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None, **kwargs):
        _FakeClient.captured = {"url": url, "headers": headers, "params": params}
        return _FakeResp(200, _FakeClient.payload)


@pytest.fixture(autouse=True)
def _market_env(monkeypatch):
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.local")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "svc-token")
    _FakeClient.captured = {}
    yield


async def _run_with(user_id: str, coro_factory):
    rc = RunContext(user_id=user_id, role="default")
    token = _current_run.set(rc)
    try:
        return await coro_factory()
    finally:
        _current_run.reset(token)


async def test_market_search_marks_joined_and_notes_locked(monkeypatch):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    _FakeClient.payload = {
        "q": "发货", "terms": ["发货"],
        "agents": [{"name": "数字中台", "type": "agent", "joined": False}],
        "skills": [{"name": "report-writer", "type": "skill", "joined": True}],
        "mcps": [], "others": [], "plugins": [],
    }
    tool = MarketSearchTool()
    out = await _run_with("web:jimingqing", lambda: tool.execute(query="发货"))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert _FakeClient.captured["url"] == "http://market.local/api/capabilities/task-search"
    assert _FakeClient.captured["headers"]["X-Act-As-Sub"] == "jimingqing"
    assert payload["agents"][0]["joined"] is False
    assert payload["skills"][0]["joined"] is True
    assert "未开通" in payload["note"] and "数字中台" in payload["note"]


async def test_market_search_joined_first_sort(monkeypatch):
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
        "web:jimingqing", lambda: MarketSearchTool().execute(query="x", kind="skill")))
    assert [i["name"] for i in out["skills"]] == ["b", "a"]
    assert "note" in out


async def test_market_search_no_note_when_all_joined(monkeypatch):
    monkeypatch.setattr(ms.httpx, "AsyncClient", _FakeClient)
    _FakeClient.payload = {
        "q": "x", "terms": [],
        "agents": [{"name": "数字中台", "joined": True}],
        "skills": [], "mcps": [], "others": [], "plugins": [],
    }
    out = json.loads(await _run_with(
        "web:jimingqing", lambda: MarketSearchTool().execute(query="x")))
    assert "note" not in out
