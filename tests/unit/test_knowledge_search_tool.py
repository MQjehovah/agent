"""知识检索工具(knowledge_search, 用户身份)单测。

覆盖:
- 逐请求携带用户 token(Bearer)调 RAG POST /api/search, 不再服务账号登录;
- 无托管 token / uid 解析失败 fail-closed 返回统一引导登录文案且不发请求;
- RAG 401 → 引导重新登录授权;
- token 解析经 asyncio.to_thread(不在事件循环线程执行同步 SSO/SQLite IO)。
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.core import RunContext, _current_run  # noqa: E402
from retrieval import RetrievalTool  # noqa: E402
from web import sso_tokens  # noqa: E402
from web.sso_tokens import USER_TOKEN_HINT as _HINT  # noqa: E402


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    captured: dict = {}
    response: tuple = (200, {})

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None, **kwargs):
        _FakeClient.captured = {"url": url, "headers": headers, "json": json}
        status, payload = _FakeClient.response
        return _FakeResp(status, payload)


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeClient.captured = {}
    _FakeClient.response = (200, {})
    yield


def _tool() -> RetrievalTool:
    tool = RetrievalTool()
    tool.configure(base_url="http://rag.local")
    return tool


async def _run_with(user_id: str, coro_factory):
    rc = RunContext(user_id=user_id, role="default")
    token = _current_run.set(rc)
    try:
        return await coro_factory()
    finally:
        _current_run.reset(token)


async def test_knowledge_search_sends_user_token(monkeypatch):
    monkeypatch.setattr("retrieval.httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "require_user_token",
                        lambda uid, audience="": f"ut-{uid}" if uid == 7 else "")
    _FakeClient.response = (200, {
        "results": [{"title": "T", "content": "C", "score": 0.9, "source": "s"}],
        "graph_expanded": 1,
    })
    out = await _run_with("web:7", lambda: _tool().execute(query="报销", top_k=3))
    payload = json.loads(out)
    assert payload["success"] is True and payload["count"] == 1
    assert payload["results"][0]["title"] == "T"
    cap = _FakeClient.captured
    assert cap["url"] == "http://rag.local/api/search"
    assert cap["headers"]["Authorization"] == "Bearer ut-7"
    assert cap["json"] == {"query": "报销", "top_k": 3}


async def test_knowledge_search_no_token_returns_hint_without_request(monkeypatch):
    """uid 不可解析 / require 返回空 → fail-closed 统一引导登录, 不发起 RAG 请求。"""
    monkeypatch.setattr("retrieval.httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "require_user_token", lambda uid, audience="": "")
    for user_id in ("", "web:朱尚荣", "web:0", "web:7"):
        out = await _run_with(user_id, lambda: _tool().execute(query="报销"))
        payload = json.loads(out)
        assert payload["success"] is False
        assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_knowledge_search_unavailable_token_returns_hint(monkeypatch):
    """require_user_token 抛 UserTokenUnavailable(无托管) → 引导文案, 不发请求。"""
    monkeypatch.setattr("retrieval.httpx.AsyncClient", _FakeClient)

    def _raise(uid, audience=""):
        raise sso_tokens.UserTokenUnavailable(_HINT)

    monkeypatch.setattr(sso_tokens, "require_user_token", _raise)
    out = await _run_with("web:7", lambda: _tool().execute(query="报销"))
    assert json.loads(out)["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_knowledge_search_401_returns_login_hint(monkeypatch):
    monkeypatch.setattr("retrieval.httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "require_user_token", lambda uid, audience="": "ut")
    _FakeClient.response = (401, {"detail": "expired"})
    out = await _run_with("web:7", lambda: _tool().execute(query="报销"))
    payload = json.loads(out)
    assert payload["success"] is False and payload["error"] == _HINT


async def test_knowledge_search_disabled_without_config(monkeypatch):
    monkeypatch.setattr("retrieval.httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "require_user_token",
                        lambda uid, audience="": pytest.fail("未配置时不应解析 token"))
    out = await _run_with("web:7", lambda: RetrievalTool().execute(query="报销"))
    payload = json.loads(out)
    assert payload["success"] is False and "未配置" in payload["error"]
    assert _FakeClient.captured == {}


async def test_knowledge_search_token_resolution_off_event_loop(monkeypatch):
    """防回归: token 解析含同步 IO, 必须经 asyncio.to_thread 执行, 不卡事件循环。"""
    monkeypatch.setattr("retrieval.httpx.AsyncClient", _FakeClient)
    loop_thread = threading.get_ident()
    marks: dict = {}

    def fake_require(uid, audience=""):
        marks["thread"] = threading.get_ident()
        marks["audience"] = audience
        return "ut"

    monkeypatch.setattr(sso_tokens, "require_user_token", fake_require)
    out = await _run_with("web:7", lambda: _tool().execute(query="x"))
    assert json.loads(out)["success"] is True
    assert marks["thread"] != loop_thread
    assert marks["audience"] == "gateway"
