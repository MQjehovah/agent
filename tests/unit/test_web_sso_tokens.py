"""web OBO: 用户 SSO token 托管/刷新/交换/缓存(_web/sso_tokens); 来源 client 决定刷新/交换身份。"""
import os
import sys
import time

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


@pytest.fixture
def fakes(monkeypatch):
    store = _FakeStorage()
    monkeypatch.setattr(sso_tokens, "_storage", lambda: store)
    monkeypatch.setattr(sso_tokens, "_cache", {})
    monkeypatch.setattr(sso_auth, "sso_downstream_audience", lambda: "gateway")
    return store


def _exp(seconds: float) -> float:
    return time.time() + seconds


def test_fresh_id_token_exchanges_and_caches(fakes, monkeypatch):
    store = fakes
    store.rows[7] = {"user_id": 7, "id_token": "id-1", "refresh_token": "rt-1",
                     "id_expires_at": _exp(600)}
    calls: list[str] = []
    monkeypatch.setattr(sso_auth, "refresh_token_grant",
                        lambda rt, **kw: pytest.fail("未过期不应刷新"))
    monkeypatch.setattr(sso_auth, "exchange_token",
                        lambda tok, aud, **kw: calls.append(f"{tok}:{aud}") or "down-1")
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(3600))

    assert sso_tokens.get_downstream_token(7) == "down-1"
    assert calls == ["id-1:gateway"]
    assert sso_tokens.get_downstream_token(7) == "down-1"  # 命中缓存, 不再交换
    assert calls == ["id-1:gateway"]


def test_near_expiry_refreshes_and_persists_rotation(fakes, monkeypatch):
    store = fakes
    store.rows[7] = {"user_id": 7, "id_token": "old", "refresh_token": "rt-1",
                     "id_expires_at": _exp(10)}  # 低于刷新余量
    monkeypatch.setattr(sso_auth, "refresh_token_grant",
                        lambda rt, **kw: {"id_token": "new-id", "refresh_token": "rt-2"})
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(600))
    monkeypatch.setattr(sso_auth, "exchange_token", lambda tok, aud, **kw: "down-2")

    assert sso_tokens.get_downstream_token(7) == "down-2"
    assert store.rows[7]["id_token"] == "new-id"
    assert store.rows[7]["refresh_token"] == "rt-2"  # 轮换后的 refresh 已回存


def test_refresh_failure_clears_tokens(fakes, monkeypatch):
    store = fakes
    store.rows[7] = {"user_id": 7, "id_token": "old", "refresh_token": "rt-dead",
                     "id_expires_at": _exp(0)}

    def _boom(rt, **kw):
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
    monkeypatch.setattr(sso_auth, "exchange_token", lambda tok, aud, **kw: next(seq))
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


def test_desktop_row_reads_id_token_and_exchanges_without_refresh(fakes, monkeypatch):
    """桌面行(client_id=dashboard-gateway)id_token 有效: 原样使用且不刷新, 交换按它执行(public 不带 secret)。"""
    store = fakes
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "s3cr3t")
    store.rows[7] = {"user_id": 7, "id_token": "id-1", "refresh_token": "rt-1",
                     "id_expires_at": _exp(600),
                     "client_id": "dashboard-gateway"}
    monkeypatch.setattr(sso_auth, "refresh_token_grant",
                        lambda rt, **kw: pytest.fail("桌面行(单一写者)不应由 agent 刷新"))
    exchange_calls: list[dict] = []

    def _exchange(tok, aud, **kw):
        exchange_calls.append({"tok": tok, "aud": aud, **kw})
        return "down-1"

    monkeypatch.setattr(sso_auth, "exchange_token", _exchange)
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(600))

    assert sso_tokens.get_downstream_token(7) == "down-1"
    assert exchange_calls == [
        {"tok": "id-1", "aud": "gateway",
         "client_id": "dashboard-gateway", "client_secret": ""}
    ]
    assert store.rows[7]["id_token"] == "id-1"  # 不刷新, 托管行保持桌面复投的原值
    assert store.rows[7]["refresh_token"] == "rt-1"


@pytest.mark.parametrize("remaining", [-5.0, 30.0])  # 已过期 / 低于 60s 余量
def test_desktop_row_unusable_id_token_fails_closed_without_refresh(fakes, monkeypatch, remaining):
    """桌面行 id_token 不可用: 返回空、不发起刷新、不删除托管行(等桌面复投)。"""
    store = fakes
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    store.rows[7] = {"user_id": 7, "id_token": "old", "refresh_token": "rt-1",
                     "id_expires_at": _exp(remaining),
                     "client_id": "dashboard-gateway"}
    monkeypatch.setattr(sso_auth, "refresh_token_grant",
                        lambda rt, **kw: pytest.fail("桌面行(单一写者)不应由 agent 刷新"))
    monkeypatch.setattr(sso_auth, "exchange_token",
                        lambda tok, aud, **kw: pytest.fail("无可用 id_token 不应发起交换"))

    assert sso_tokens.get_downstream_token(7) == ""
    assert store.rows[7]["id_token"] == "old"  # 托管行未删除
    assert store.rows[7]["refresh_token"] == "rt-1"


def test_default_agent_row_defers_to_agent_config(fakes, monkeypatch):
    """老行/agent 行: 客户端取 agent, secret=None(由 sso_auth 用自身配置), 默认路径零漂移。"""
    store = fakes
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "s3cr3t")
    store.rows[7] = {"user_id": 7, "id_token": "id-1", "refresh_token": "rt",
                     "id_expires_at": _exp(600)}  # 无 client_id(老行)
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(3600))
    calls: list[dict] = []
    monkeypatch.setattr(sso_auth, "exchange_token",
                        lambda tok, aud, **kw: calls.append(kw) or "down-1")

    assert sso_tokens.get_downstream_token(7) == "down-1"
    assert calls == [{"client_id": "agent", "client_secret": None}]


def test_save_user_tokens_persists_client_id(fakes, monkeypatch):
    store = fakes
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(600))
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    assert sso_tokens.save_user_tokens(
        5, {"id_token": "id", "refresh_token": "rt", "client_id": "dashboard-gateway"}
    ) is True
    assert store.rows[5]["client_id"] == "dashboard-gateway"
    assert sso_tokens.save_user_tokens(6, {"id_token": "id", "refresh_token": "rt"}) is True
    assert store.rows[6]["client_id"] == "agent"  # 缺省=配置客户端(web 回调路径)


def test_save_user_tokens_default_client_follows_config(fakes, monkeypatch):
    """写入缺省跟随 SSO_CLIENT_ID 配置(自定义部署读写一致)。"""
    store = fakes
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(600))
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "xyz")
    assert sso_tokens.save_user_tokens(5, {"id_token": "id", "refresh_token": "rt"}) is True
    assert store.rows[5]["client_id"] == "xyz"


def test_client_creds_follow_config_for_legacy_agent_rows(fakes, monkeypatch):
    """自定义 SSO_CLIENT_ID=xyz: 配置行与遗留 agent 行都解析为配置客户端(用配置 secret);
    dashboard-gateway 行按 public 不带 secret。"""
    store = fakes
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "xyz")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "s3cr3t")
    monkeypatch.setattr(sso_auth, "token_exp", lambda tok: _exp(3600))
    calls: list[dict] = []
    monkeypatch.setattr(sso_auth, "exchange_token",
                        lambda tok, aud, **kw: calls.append(kw) or "down")
    for uid, cid in ((7, "xyz"), (8, "agent"), (9, "dashboard-gateway")):
        store.rows[uid] = {"user_id": uid, "id_token": f"id-{uid}", "refresh_token": "rt",
                           "id_expires_at": _exp(600), "client_id": cid}
        assert sso_tokens.get_downstream_token(uid) == "down"
    assert calls == [
        {"client_id": "xyz", "client_secret": None},  # 配置行: 交配置(secret 用配置值)
        {"client_id": "xyz", "client_secret": None},  # 遗留 agent 行: 同样解析为配置客户端
        {"client_id": "dashboard-gateway", "client_secret": ""},  # public: 不带 secret
    ]
