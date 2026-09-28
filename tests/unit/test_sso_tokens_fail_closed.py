"""web OBO fail-closed 入口(sso_tokens.require_user_token)单测。

覆盖:
- 无托管 token → 抛 UserTokenUnavailable, 文案含统一引导(「登录一次」);
- 有托管 token(mock get_downstream_token) → 原样返回。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from web import sso_auth, sso_tokens  # noqa: E402


class _FakeStorage:
    def __init__(self):
        self.rows: dict[int, dict] = {}

    def save_sso_tokens(self, uid, id_token, refresh_token, id_expires_at, client_id="agent"):
        self.rows[uid] = {"user_id": uid, "id_token": id_token, "refresh_token": refresh_token,
                          "id_expires_at": id_expires_at, "client_id": client_id}

    def get_sso_tokens(self, uid):
        return self.rows.get(uid)

    def delete_sso_tokens(self, uid):
        self.rows.pop(uid, None)


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    store = _FakeStorage()
    monkeypatch.setattr(sso_tokens, "_storage", lambda: store)
    monkeypatch.setattr(sso_tokens, "_cache", {})
    monkeypatch.setattr(sso_auth, "sso_downstream_audience", lambda: "dashboard-gateway")
    return store


def test_require_user_token_raises_with_login_hint_when_unhosted(_fakes):
    with pytest.raises(sso_tokens.UserTokenUnavailable) as ei:
        sso_tokens.require_user_token(7)
    assert "登录一次" in str(ei.value)


def test_require_user_token_returns_hosted_token(monkeypatch):
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="": calls.append((uid, audience)) or "tok-1")
    assert sso_tokens.require_user_token(7, "gateway") == "tok-1"
    assert calls == [(7, "gateway")]
