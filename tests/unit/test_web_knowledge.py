"""web 知识库代理: 用户 SSO 交换轨优先 + 服务账号兜底 + 401 重换重试。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import sso_tokens  # noqa: E402
from web.routers import knowledge as knowledge_mod  # noqa: E402


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def env(monkeypatch):
    calls: dict = {"direct": [], "service": []}

    def fake_configure(self):
        self._base = "http://rag.test"
        self._loaded = True
        return self._base

    def fake_service_request(self, method, path, *, params=None, json_body=None):
        calls["service"].append({"method": method, "path": path})
        return _Resp(200, {"via": "service"}), None

    def fake_direct(method, url, token, *, params=None, json_body=None):
        calls["direct"].append({"method": method, "url": url, "token": token})
        return _Resp(200, {"via": "user"})

    monkeypatch.setattr(knowledge_mod._RagClient, "configure", fake_configure)
    monkeypatch.setattr(knowledge_mod._RagClient, "request", fake_service_request)
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
    assert calls["service"] == []


def test_fallback_service_when_no_user_token(env, monkeypatch):
    client, calls = env
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "")
    r = client.get("/api/knowledge/spaces")
    assert r.status_code == 200 and r.json()["via"] == "service"
    assert calls["direct"] == [] and len(calls["service"]) == 1


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
