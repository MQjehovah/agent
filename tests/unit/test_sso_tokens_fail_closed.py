"""web OBO fail-closed 入口(sso_tokens.require_user_token)与工具公共解析单测。

覆盖:
- 无托管 token → 抛 UserTokenUnavailable, 文案为统一引导(USER_TOKEN_HINT);
- 有托管 token(mock get_downstream_token) → 原样返回;
- tools.user_token.resolve_user_token_or_hint: uid 解析 + audience 恒为 gateway,
  无托管/意外异常一律 fail-closed 返回引导文案。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.core import RunContext, _current_run  # noqa: E402
from tools import user_token  # noqa: E402
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
    monkeypatch.setattr(sso_auth, "sso_downstream_audience", lambda: "gateway")
    return store


def test_require_user_token_raises_with_login_hint_when_unhosted(_fakes):
    with pytest.raises(sso_tokens.UserTokenUnavailable) as ei:
        sso_tokens.require_user_token(7)
    assert str(ei.value) == sso_tokens.USER_TOKEN_HINT


def test_require_user_token_returns_hosted_token(monkeypatch):
    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="": calls.append((uid, audience)) or "tok-1")
    assert sso_tokens.require_user_token(7, "gateway") == "tok-1"
    assert calls == [(7, "gateway")]


# ---- tools.user_token.resolve_user_token_or_hint(工具侧公共解析) ----


def _in_run_ctx(user_id: str, func):
    rc = RunContext(user_id=user_id, role="default")
    token = _current_run.set(rc)
    try:
        return func()
    finally:
        _current_run.reset(token)


def test_resolve_user_token_parses_uid_and_keeps_gateway_audience(monkeypatch):
    """防回归: 必须按 tag 数字 uid 调用, audience 恒为 gateway(不得回退默认受众)。"""
    calls: list[tuple[int, str]] = []

    def fake_require(uid, audience=""):
        calls.append((uid, audience))
        return "tok-7"

    monkeypatch.setattr(sso_tokens, "require_user_token", fake_require)
    assert _in_run_ctx("web:7", user_token.resolve_user_token_or_hint) == ("tok-7", None)
    assert calls == [(7, "gateway")]


def test_resolve_user_token_fail_closed_on_unavailable_token(monkeypatch):
    def fake_require(uid, audience=""):
        raise sso_tokens.UserTokenUnavailable(sso_tokens.USER_TOKEN_HINT)

    monkeypatch.setattr(sso_tokens, "require_user_token", fake_require)
    assert _in_run_ctx("web:7", user_token.resolve_user_token_or_hint) == ("", sso_tokens.USER_TOKEN_HINT)


def test_resolve_user_token_fail_closed_on_empty_token(monkeypatch):
    """防回归: require 返回空串(异常路径)同样 fail-closed, 不把空 Bearer 透传给下游。"""
    monkeypatch.setattr(sso_tokens, "require_user_token", lambda uid, audience="": "")
    assert _in_run_ctx("web:7", user_token.resolve_user_token_or_hint) == ("", sso_tokens.USER_TOKEN_HINT)


def test_resolve_user_token_fail_closed_on_unexpected_error(monkeypatch):
    """意外异常(刷新/交换/DB 等) 同样 fail-closed 并返回引导文案, 不外漏原生异常。"""
    def fake_require(uid, audience=""):
        raise RuntimeError("boom")

    monkeypatch.setattr(sso_tokens, "require_user_token", fake_require)
    assert _in_run_ctx("web:7", user_token.resolve_user_token_or_hint) == ("", sso_tokens.USER_TOKEN_HINT)


def test_resolve_user_token_fail_closed_on_unparsable_uid(monkeypatch):
    monkeypatch.setattr(sso_tokens, "require_user_token",
                        lambda uid, audience="": pytest.fail("uid 不可解析时不应调用 require_user_token"))
    for user_id in ("", "web:朱尚荣"):
        assert _in_run_ctx(user_id, user_token.resolve_user_token_or_hint) == ("", sso_tokens.USER_TOKEN_HINT)
