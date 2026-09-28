"""web 算力用量代理: 用户 OBO 交换 + 网关响应透传 + 无会话 401 + 401 重换。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import sso_tokens  # noqa: E402
from web.routers import gateway_usage as gu  # noqa: E402


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def env(monkeypatch):
    calls: dict = {"urls": [], "tokens": []}

    def fake_get(url, token):
        calls["urls"].append(url)
        calls["tokens"].append(token)
        return _Resp(200, {"balance": 1, "today": {"tokens": 2}})

    monkeypatch.setattr(gu, "_get_json", fake_get)
    monkeypatch.setattr(gu, "get_authz", lambda request: {"uid": 7})
    monkeypatch.setattr(gu, "gateway_admin_base", lambda: "http://gw.test")
    app = FastAPI()
    app.include_router(gu.build_gateway_usage_router(None))
    return TestClient(app), calls


def test_gateway_usage_passthrough(env, monkeypatch):
    client, calls = env
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "gt" if uid == 7 else "")
    r = client.get("/api/me/gateway-usage")
    assert r.status_code == 200 and r.json()["balance"] == 1
    assert calls["urls"] == ["http://gw.test/api/me/usage"]
    assert calls["tokens"] == ["gt"]


def test_gateway_usage_401_retries_with_forced_exchange(env, monkeypatch):
    client, calls = env

    def fake_get(url, token):
        calls["tokens"].append(token)
        if token == "t1":
            return _Resp(401, {"error": "expired"})
        return _Resp(200, {"balance": 2})

    monkeypatch.setattr(gu, "_get_json", fake_get)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "t2" if force else "t1")
    r = client.get("/api/me/gateway-usage")
    assert r.status_code == 200 and r.json()["balance"] == 2
    assert calls["tokens"] == ["t1", "t2"]


def test_gateway_usage_no_sso_session_401(env, monkeypatch):
    client, calls = env
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", force=False: "")
    r = client.get("/api/me/gateway-usage")
    assert r.status_code == 401 and "重新登录" in r.json()["error"]
    assert calls["tokens"] == []
