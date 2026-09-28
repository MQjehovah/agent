"""web OBO: 用户 SSO token 托管/刷新/交换/缓存(_web/sso_tokens)。"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from web import sso_auth, sso_tokens  # noqa: E402


class _FakeStorage:
    def __init__(self):
        self.rows: dict[int, dict] = {}

    def save_sso_tokens(self, uid, id_token, refresh_token, id_expires_at):
        self.rows[uid] = {"user_id": uid, "id_token": id_token,
                          "refresh_token": refresh_token, "id_expires_at": id_expires_at}

    def get_sso_tokens(self, uid):
        return self.rows.get(uid)

    def delete_sso_tokens(self, uid):
        self.rows.pop(uid, None)


@pytest.fixture
def fakes(monkeypatch):
    store = _FakeStorage()
    monkeypatch.setattr(sso_tokens, "_storage", lambda: store)
    monkeypatch.setattr(sso_tokens, "_cache", {})
    monkeypatch.setattr(sso_auth, "sso_downstream_audience", lambda: "dashboard-gateway")
    return store


def _exp(seconds: float) -> float:
    return time.time() + seconds


def test_fresh_id_token_exchanges_and_caches(fakes, monkeypatch):
    store = fakes
    store.rows[7] = {"user_id": 7, "id_token": "id-1", "refresh_token": "rt-1",
                     "id_expires_at": _exp(600)}
    calls: list[str] = []
    monkeypatch.setattr(sso_auth, "refresh_token_grant",
                        lambda rt: pytest.fail("未过期不应刷新"))
    monkeypatch.setattr(sso_auth, "exchange_token",
                        lambda tok, aud: calls.append(f"{tok}:{aud}") or "down-1")
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(3600))

    assert sso_tokens.get_downstream_token(7) == "down-1"
    assert calls == ["id-1:dashboard-gateway"]
    assert sso_tokens.get_downstream_token(7) == "down-1"  # 命中缓存, 不再交换
    assert calls == ["id-1:dashboard-gateway"]


def test_near_expiry_refreshes_and_persists_rotation(fakes, monkeypatch):
    store = fakes
    store.rows[7] = {"user_id": 7, "id_token": "old", "refresh_token": "rt-1",
                     "id_expires_at": _exp(10)}  # 低于刷新余量
    monkeypatch.setattr(sso_auth, "refresh_token_grant",
                        lambda rt: {"id_token": "new-id", "refresh_token": "rt-2"})
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(600))
    monkeypatch.setattr(sso_auth, "exchange_token", lambda tok, aud: "down-2")

    assert sso_tokens.get_downstream_token(7) == "down-2"
    assert store.rows[7]["id_token"] == "new-id"
    assert store.rows[7]["refresh_token"] == "rt-2"  # 轮换后的 refresh 已回存


def test_refresh_failure_clears_tokens(fakes, monkeypatch):
    store = fakes
    store.rows[7] = {"user_id": 7, "id_token": "old", "refresh_token": "rt-dead",
                     "id_expires_at": _exp(0)}

    def _boom(rt):
        raise sso_auth.SsoAuthError("invalid_grant")

    monkeypatch.setattr(sso_auth, "refresh_token_grant", _boom)
    assert sso_tokens.get_downstream_token(7) == ""
    assert 7 not in store.rows


def test_missing_or_zero_uid_returns_empty(fakes):
    assert sso_tokens.get_downstream_token(0) == ""
    assert sso_tokens.get_downstream_token(9) == ""


def test_force_skips_cache(fakes, monkeypatch):
    store = fakes
    store.rows[7] = {"user_id": 7, "id_token": "id-1", "refresh_token": "rt",
                     "id_expires_at": _exp(600)}
    seq = iter(["down-a", "down-b"])
    monkeypatch.setattr(sso_auth, "exchange_token", lambda tok, aud: next(seq))
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(3600))

    assert sso_tokens.get_downstream_token(7) == "down-a"
    assert sso_tokens.get_downstream_token(7, force=True) == "down-b"


def test_save_and_clear(fakes, monkeypatch):
    store = fakes
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(600))
    assert sso_tokens.save_user_tokens(5, {"id_token": "id", "refresh_token": "rt"}) is True
    assert store.rows[5]["id_token"] == "id" and store.rows[5]["refresh_token"] == "rt"
    sso_tokens.clear_user_tokens(5)
    assert 5 not in store.rows
