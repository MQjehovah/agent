"""web 知识库代理: 用户 SSO 交换轨(401 重换重试) + 无 token fail-closed 503 引导登录。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import sso_tokens  # noqa: E402
from web.routers import knowledge as knowledge_mod  # noqa: E402
from web.sso_tokens import USER_TOKEN_HINT as _HINT  # noqa: E402


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def env(monkeypatch):
    calls: dict = {"direct": []}

    def fake_configure(self):
        self._base = "http://rag.test"
        self._loaded = True
        return self._base

    def fake_direct(method, url, token, *, params=None, json_body=None):
        calls["direct"].append({"method": method, "url": url, "token": token})
        return _Resp(200, {"via": "user"})

    monkeypatch.setattr(knowledge_mod._RagConfig, "configure", fake_configure)
    monkeypatch.setattr(knowledge_mod, "_direct_request", fake_direct)
    monkeypatch.setattr(knowledge_mod, "get_authz", lambda request: {"uid": 7})

    app = FastAPI()
    app.include_router(knowledge_mod.build_knowledge_router(None))
    return TestClient(app), calls


def test_user_track_preferred(env, monkeypatch):
    client, calls = env
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "ut" if uid == 7 else "")
    r = client.get("/api/knowledge/spaces")
    assert r.status_code == 200 and r.json()["via"] == "user"
    assert calls["direct"] == [{"method": "GET",
                                "url": "http://rag.test/api/wiki/spaces",
                                "token": "ut"}]


def test_no_user_token_returns_503_hint_without_request(env, monkeypatch):
    """无 SSO 会话(本地密码/服务身份) → fail-closed: 503 + 引导文案, 不发起下游请求。"""
    client, calls = env
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "")
    for path in ("/api/knowledge/spaces", "/api/knowledge/wiki",
                 "/api/knowledge/wiki/p1", "/api/knowledge/search?q=x"):
        r = client.get(path)
        assert r.status_code == 503, path
        assert r.json()["error"] == _HINT, path
    assert calls["direct"] == []


def test_service_identity_uid0_returns_503_hint(env, monkeypatch):
    """服务身份(uid=0)同样无用户 token → 503 引导登录, 不串服务账号视角。"""
    client, calls = env
    monkeypatch.setattr(knowledge_mod, "get_authz", lambda request: {"uid": 0})
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "ut")
    r = client.get("/api/knowledge/spaces")
    assert r.status_code == 503
    assert r.json()["error"] == _HINT
    assert calls["direct"] == []


def test_user_track_401_retries_with_forced_exchange(env, monkeypatch):
    client, calls = env

    def fake_token(uid, audience="", force=False):
        return "t2" if force else "t1"

    def fake_direct(method, url, token, *, params=None, json_body=None):
        calls["direct"].append(token)
        if token == "t1":
            return _Resp(401, {"detail": "expired"})
        return _Resp(200, {"via": "user"})

    monkeypatch.setattr(sso_tokens, "get_downstream_token", fake_token)
    monkeypatch.setattr(knowledge_mod, "_direct_request", fake_direct)
    r = client.get("/api/knowledge/wiki")
    assert r.status_code == 200
    assert calls["direct"] == ["t1", "t2"]


def test_401_after_retry_returns_hint(env, monkeypatch):
    """重换后仍 401 → 引导重新登录授权(不泄漏下游错误, 不回退服务账号)。"""
    client, calls = env
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "t1")

    def fake_direct(method, url, token, *, params=None, json_body=None):
        calls["direct"].append(token)
        return _Resp(401, {"detail": "expired"})

    monkeypatch.setattr(knowledge_mod, "_direct_request", fake_direct)
    r = client.get("/api/knowledge/spaces")
    assert r.status_code == 503
    assert r.json()["error"] == _HINT
    assert calls["direct"] == ["t1", "t1"]
