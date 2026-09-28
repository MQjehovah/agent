"""桌面 SSO token 托管接口 /api/auth/sso-tokens。

覆盖:
- 登录用户(JWT)托管合法签名 id_token 后 sso_tokens 表出现该 uid 记录;
- 未登录 401; 缺/非字符串 id_token 400; 伪造签名 400;
- sub 与登录态 work_id 不匹配 403(防托管他人 token);
- 桌面通道只验签+iss: aud 非 agent 受众、已过期(短 TTL)的 id_token 仍可托管;
- 不同用户各存各的(隔离), body 内 uid 被忽略(仅允许写本人)。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk as jose_jwk  # noqa: E402

import storage.storage as storage_mod  # noqa: E402
from storage.storage import Storage  # noqa: E402
from web import sso_auth  # noqa: E402
from web.server import WebServer, create_jwt  # noqa: E402

ISSUER = "http://192.168.31.45:8091"
AGENT_AUDIENCE = "agent"
# 桌面 id_token 的 aud 是工作台客户端, 与 agent 受众不同
DESKTOP_AUDIENCE = "dashboard-gateway"


def make_rsa_key(kid="k1"):
    """生成 RSA2048 私钥,返回 (私钥, 公钥 JWK dict)。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jose_jwk.RSAKey(key.public_key(), algorithm="RS256").to_dict()
    public_jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, public_jwk


def sign_token(claims, key, kid="k1"):
    """用 pyjwt 以 RS256 自签 token。"""
    return pyjwt.encode(claims, key, algorithm="RS256", headers={"kid": kid} if kid else None)


def id_claims(sub: str = "10086", *, aud: str = DESKTOP_AUDIENCE, exp_delta: int = 600) -> dict:
    now = int(time.time())
    return {"sub": sub, "iss": ISSUER, "aud": aud, "iat": now, "exp": now + exp_delta, "name": "张三"}


@pytest.fixture(autouse=True)
def _reset_jwks_cache():
    """每个测试前后重置 sso_auth JWKS 模块级缓存,避免跨测试污染。"""

    def _reset():
        sso_auth._jwks_cache["data"] = None
        sso_auth._jwks_cache["fetched_at"] = 0.0
        sso_auth._jwks_cache["uri"] = ""

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _auth_enabled(monkeypatch):
    """确保走真实鉴权路径(其它测试文件可能置 1; 模块导入期改 env 会泄漏给后续测试)。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")


@pytest.fixture
def sso_env(monkeypatch, tmp_path):
    """RSA 私钥 + 本地 JWKS 文件并打开 SSO 配置; 返回私钥。"""
    key, public_jwk = make_rsa_key()
    jwks_path = tmp_path / "jwks.json"
    jwks_path.write_text(json.dumps({"keys": [public_jwk]}), encoding="utf-8")
    monkeypatch.setenv("SSO_ISSUER", ISSUER)
    monkeypatch.setenv("SSO_AUDIENCE", AGENT_AUDIENCE)
    monkeypatch.setenv("SSO_JWKS_URI", jwks_path.as_uri())
    return key


@pytest.fixture
def env(tmp_path, sso_env):
    """临时 Storage(内置 uid 7/8 用户) + WebServer/TestClient; 返回 (storage, client, 私钥)。"""
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    with s.get_connection() as conn:
        for uid, name, work_id in ((7, "张三", "10086"), (8, "李四", "10087")):
            conn.execute(
                "INSERT INTO rbac_users (id, name, work_id, department, role, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?, datetime('now'), datetime('now'))",
                (uid, name, work_id, "研发", "default", "active"),
            )
        conn.commit()
    w = WebServer()
    client = TestClient(w._app)
    yield s, client, sso_env
    s.close()
    storage_mod._storage_instance = prev


def _auth(uid: int, work_id: str = "", name: str = "张三", role: str = "default") -> dict:
    user: dict = {"id": uid, "name": name, "role": role}
    if work_id:
        user["work_id"] = work_id
    return {"Authorization": f"Bearer {create_jwt(user)}"}


def _row(storage: Storage, uid: int) -> dict | None:
    with storage.get_connection() as conn:
        r = conn.execute(
            "SELECT id_token, refresh_token FROM sso_tokens WHERE user_id=?", (uid,)
        ).fetchone()
    return dict(r) if r else None


def test_host_tokens_saves_current_user_tokens(env):
    s, client, key = env
    id_token = sign_token(id_claims("10086"), key)
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": id_token, "refresh_token": "ref-7"},
        headers=_auth(7, "10086"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert _row(s, 7) == {"id_token": id_token, "refresh_token": "ref-7"}


def test_host_tokens_overwrite_is_idempotent(env):
    """续期复投: 同一用户重复托管覆盖旧值。"""
    s, client, key = env
    old = sign_token(id_claims("10086"), key)
    new = sign_token(id_claims("10086"), key)
    client.post("/api/auth/sso-tokens", json={"id_token": old, "refresh_token": "ref-old"}, headers=_auth(7, "10086"))
    resp = client.post(
        "/api/auth/sso-tokens", json={"id_token": new, "refresh_token": "ref-new"}, headers=_auth(7)
    )
    assert resp.status_code == 200, resp.text
    assert _row(s, 7) == {"id_token": new, "refresh_token": "ref-new"}


def test_host_tokens_requires_auth(env):
    s, client, key = env
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": sign_token(id_claims("10086"), key), "refresh_token": "ref"},
    )
    assert resp.status_code == 401
    assert _row(s, 7) is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"id_token": ""},
        {"id_token": "   ", "refresh_token": "r"},
        {"id_token": 123},
        {"id_token": {"nested": "x"}},
    ],
)
def test_host_tokens_requires_string_id_token(env, body):
    s, client, _ = env
    resp = client.post("/api/auth/sso-tokens", json=body, headers=_auth(7, "10086"))
    assert resp.status_code == 400, resp.text
    assert _row(s, 7) is None


def test_host_tokens_rejects_non_string_refresh_token(env):
    s, client, key = env
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": sign_token(id_claims("10086"), key), "refresh_token": 123},
        headers=_auth(7, "10086"),
    )
    assert resp.status_code == 400, resp.text
    assert _row(s, 7) is None


def test_host_tokens_rejects_bad_signature(env):
    """另一把 RSA 私钥签的 id_token(签名不通过 JWKS 验签) → 400, 不落库。"""
    s, client, _ = env
    other_key, _ = make_rsa_key()
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": sign_token(id_claims("10086"), other_key), "refresh_token": "ref"},
        headers=_auth(7, "10086"),
    )
    assert resp.status_code == 400, resp.text
    assert _row(s, 7) is None


def test_host_tokens_rejects_subject_mismatch(env):
    """他人 sub 的合法签名 id_token: 403, 不落库。"""
    s, client, key = env
    id_token = sign_token(id_claims("10087"), key)  # 李四的工号
    resp = client.post(
        "/api/auth/sso-tokens", json={"id_token": id_token, "refresh_token": "ref"}, headers=_auth(7)
    )
    assert resp.status_code == 403, resp.text
    assert _row(s, 7) is None


def test_host_tokens_falls_back_to_db_work_id(env):
    """JWT 未携带 work_id 时以 DB(rbac_users)工号为绑定依据。"""
    s, client, key = env
    id_token = sign_token(id_claims("10086"), key)
    resp = client.post(
        "/api/auth/sso-tokens", json={"id_token": id_token, "refresh_token": "ref-7"}, headers=_auth(7)
    )
    assert resp.status_code == 200, resp.text
    assert _row(s, 7) == {"id_token": id_token, "refresh_token": "ref-7"}


def test_host_tokens_accepts_desktop_audience_and_expired_token(env):
    """桌面通道: aud≠agent 受众且已过期的 id_token(短 TTL)仍可托管, sub 绑定本人即通过。"""
    s, client, key = env
    id_token = sign_token(id_claims("10086", aud=DESKTOP_AUDIENCE, exp_delta=-60), key)
    resp = client.post(
        "/api/auth/sso-tokens", json={"id_token": id_token, "refresh_token": "ref-7"}, headers=_auth(7)
    )
    assert resp.status_code == 200, resp.text
    assert _row(s, 7) == {"id_token": id_token, "refresh_token": "ref-7"}


def test_host_tokens_isolated_between_users(env):
    s, client, key = env
    tok_7 = sign_token(id_claims("10086"), key)
    tok_8 = sign_token(id_claims("10087"), key)
    r7 = client.post(
        "/api/auth/sso-tokens", json={"id_token": tok_7, "refresh_token": "ref-7"}, headers=_auth(7)
    )
    r8 = client.post(
        "/api/auth/sso-tokens", json={"id_token": tok_8, "refresh_token": "ref-8"}, headers=_auth(8, "10087")
    )
    assert (r7.status_code, r8.status_code) == (200, 200)
    assert _row(s, 7) == {"id_token": tok_7, "refresh_token": "ref-7"}
    assert _row(s, 8) == {"id_token": tok_8, "refresh_token": "ref-8"}


def test_host_tokens_ignores_body_uid(env):
    """uid 取自登录态, body 里的 uid 不生效(防越权代写)。"""
    s, client, key = env
    id_token = sign_token(id_claims("10086"), key)
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": id_token, "refresh_token": "ref-7", "uid": 9},
        headers=_auth(7, "10086"),
    )
    assert resp.status_code == 200, resp.text
    assert _row(s, 7) == {"id_token": id_token, "refresh_token": "ref-7"}
    assert _row(s, 9) is None
