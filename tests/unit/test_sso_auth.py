"""SSO/OIDC token 校验 + server 双轨鉴权测试。

用临时 RSA 自签 token + file:// JWKS 验证 verify_sso_token(同 rag/market 思路),
并补一盏 /api/auth/me 双轨(_get_auth HS256 失败后走 SSO)的最小用例。

依赖: pyjwt(自签 RS256)、python-jose[cryptography](验签), 均已安装。
"""

import base64
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import jwt as pyjwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from jose import jwk as jose_jwk  # noqa: E402

from web import sso_auth  # noqa: E402
from web.sso_auth import SsoAuthError, verify_sso_token  # noqa: E402

ISSUER = "http://192.168.31.45:8091"
AUDIENCE = "agent"


def make_rsa_key(kid="k1"):
    """生成 RSA2048 私钥,返回 (私钥, 公钥 JWK dict)。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jose_jwk.RSAKey(key.public_key(), algorithm="RS256").to_dict()
    public_jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return key, public_jwk


def write_jwks(tmp_path, *public_jwks):
    """把若干公钥 JWK 写成临时 JWKS 文件,返回其 file:// URI。"""
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps({"keys": list(public_jwks)}), encoding="utf-8")
    return path.as_uri()


def sign_token(payload, key, kid="k1"):
    """用 pyjwt 以 RS256 自签 token。"""
    headers = {"kid": kid} if kid else None
    return pyjwt.encode(payload, key, algorithm="RS256", headers=headers)


def valid_claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": "10086",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
        "name": "张三",
        "dept": "研发",
        "roles": ["user"],
    }
    claims.update(overrides)
    return claims


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


@pytest.fixture
def sso_env(monkeypatch, tmp_path):
    """生成密钥+JWKS 并打开 SSO 配置(env),返回 (私钥, 公钥 JWK dict)。"""
    key, public_jwk = make_rsa_key()
    jwks_uri = write_jwks(tmp_path, public_jwk)
    monkeypatch.setenv("SSO_ISSUER", ISSUER)
    monkeypatch.setenv("SSO_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("SSO_JWKS_URI", jwks_uri)
    return key, public_jwk


# ---- verify_sso_token ----

def test_verify_sso_token_accepts_valid_token(sso_env):
    key, _ = sso_env
    claims = verify_sso_token(sign_token(valid_claims(), key))
    assert claims["sub"] == "10086"
    assert claims["iss"] == ISSUER
    assert claims["aud"] == AUDIENCE


def test_verify_sso_token_rejects_bad_signature(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token + "tampered")


def test_verify_sso_token_rejects_wrong_audience(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(aud="some-other-app"), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)


def test_verify_sso_token_rejects_expired_token(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(exp=int(time.time()) - 60), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)


def test_verify_sso_token_rejects_wrong_issuer(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(iss="https://evil.example.com"), key)
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)


def test_verify_sso_token_disabled_when_issuer_empty(monkeypatch):
    monkeypatch.delenv("SSO_ISSUER", raising=False)
    monkeypatch.delenv("SSO_JWKS_URI", raising=False)
    with pytest.raises(SsoAuthError, match="[Nn]ot configured"):
        verify_sso_token("not.even.a.jwt")


def test_verify_sso_token_falls_back_to_first_key_without_kid(sso_env):
    key, _ = sso_env
    token = sign_token(valid_claims(), key, kid=None)
    assert verify_sso_token(token)["sub"] == "10086"


def test_verify_sso_token_selects_key_by_kid(monkeypatch, tmp_path):
    key1, jwk1 = make_rsa_key(kid="k1")
    key2, jwk2 = make_rsa_key(kid="k2")
    jwks_uri = write_jwks(tmp_path, jwk1, jwk2)
    monkeypatch.setenv("SSO_ISSUER", ISSUER)
    monkeypatch.setenv("SSO_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("SSO_JWKS_URI", jwks_uri)
    token = sign_token(valid_claims(), key2, kid="k2")
    assert verify_sso_token(token)["sub"] == "10086"


def test_verify_sso_token_rejects_unknown_kid(monkeypatch, tmp_path):
    key1, jwk1 = make_rsa_key(kid="k1")
    _, jwk2 = make_rsa_key(kid="k2")
    jwks_uri = write_jwks(tmp_path, jwk1, jwk2)
    monkeypatch.setenv("SSO_ISSUER", ISSUER)
    monkeypatch.setenv("SSO_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("SSO_JWKS_URI", jwks_uri)
    token = sign_token(valid_claims(), key1, kid="ghost")
    with pytest.raises(SsoAuthError):
        verify_sso_token(token)


# ---- authorize URL / state ----

def test_build_authorize_url_contains_required_params(sso_env):
    url = sso_auth.build_authorize_url("sT4t3")
    assert url.startswith(ISSUER + "/authorize?")
    assert "response_type=code" in url
    assert "client_id=agent" in url
    assert "state=sT4t3" in url
    assert "scope=openid+profile" in url
    assert "offline_access" in url


def test_build_authorize_url_disabled_without_issuer(monkeypatch):
    monkeypatch.delenv("SSO_ISSUER", raising=False)
    with pytest.raises(SsoAuthError):
        sso_auth.build_authorize_url("x")


def test_state_is_one_time_and_ttl_bound():
    st = sso_auth.new_state()
    assert sso_auth.validate_state(st) is True
    assert sso_auth.validate_state(st) is False  # 一次性


def test_state_rejects_unknown():
    assert sso_auth.validate_state("no-such-state") is False


# ---- SSO HTTP 端点(最小覆盖, 不依赖真实 SSO) ----

def test_sso_start_disabled_returns_404(monkeypatch):
    monkeypatch.delenv("SSO_ISSUER", raising=False)
    monkeypatch.delenv("SSO_JWKS_URI", raising=False)
    from fastapi.testclient import TestClient

    from web.server import WebServer

    w = WebServer()
    client = TestClient(w._app)
    assert client.get("/api/auth/sso/start").status_code == 404


def test_sso_start_redirects_when_configured(sso_env):
    from fastapi.testclient import TestClient

    from web.server import WebServer

    w = WebServer()
    client = TestClient(w._app, follow_redirects=False)
    resp = client.get("/api/auth/sso/start")
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith(ISSUER + "/authorize?")
    assert "state=" in loc


def test_sso_callback_rejects_bad_state(sso_env, monkeypatch):
    """state 失效时跳回前端登录页并带 error,不把 JSON 错误丢给浏览器。"""
    from fastapi.testclient import TestClient

    from web.server import WebServer

    monkeypatch.setenv("SSO_REDIRECT_TARGET", "/agent/#/login")
    w = WebServer()
    client = TestClient(w._app, follow_redirects=False)
    resp = client.get("/api/auth/sso/callback", params={"code": "abc", "state": "bad"})
    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert loc.startswith("/agent/#/login?error=")
    assert "sso_token=" not in loc


# ---- server _get_auth 双轨(HS256 失败 → SSO 兜底) ----

def test_get_auth_dual_track_accepts_sso_token(sso_env, tmp_path, monkeypatch):
    """SSO(RS256) token 经 _get_auth 兜底转工号→查用户成功(带 access-token 直调)。"""
    from fastapi.testclient import TestClient

    import storage.storage as storage_mod
    from storage.storage import Storage
    from web.server import WebServer

    key, _ = sso_env
    # 预置 rbac 用户(work_id=sub 工号, name=姓名)
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    with s.get_connection() as conn:
        conn.execute(
            "INSERT INTO rbac_users (name, work_id, department, role, status, created_at, updated_at) "
            "VALUES ('张三', '10086', '研发', 'default', 'active', datetime('now'), datetime('now'))"
        )
        conn.commit()
    try:
        w = WebServer()
        client = TestClient(w._app)
        token = sign_token(valid_claims(), key)
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "张三"
        assert resp.json()["work_id"] == "10086"
        assert resp.json()["role"] == "default"
    finally:
        s.close()
        storage_mod._storage_instance = prev


def test_get_auth_dual_track_rejects_invalid_sso_token(sso_env, tmp_path):
    """非法 SSO token 回落 401(不因双轨而放行非法凭证)。"""
    from fastapi.testclient import TestClient

    import storage.storage as storage_mod
    from storage.storage import Storage
    from web.server import WebServer

    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    try:
        w = WebServer()
        client = TestClient(w._app)
        resp = client.get("/api/auth/me", headers={"Authorization": "Bearer bad.token.value"})
        assert resp.status_code == 401
    finally:
        s.close()
        storage_mod._storage_instance = prev


# ---- RFC 8693 token exchange(零号员工代授权) ----


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def test_exchange_token_posts_rfc8693_and_returns_access_token(monkeypatch):
    captured: dict = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp(b'{"access_token":"downstream-token"}')

    monkeypatch.setattr(sso_auth.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sso_auth, "sso_issuer", lambda: "https://sso.example.com")
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "s3cr3t")

    out = sso_auth.exchange_token("subject-abc", "market")
    assert out == "downstream-token"
    body = captured["data"].decode("utf-8")
    assert "grant_type=" + urllib.parse.quote(sso_auth.TOKEN_EXCHANGE_GRANT, safe="") in body
    assert "subject_token=subject-abc" in body
    assert "audience=market" in body
    assert captured["url"] == "https://sso.example.com/token"
    assert captured["auth"].startswith("Basic ")


def test_exchange_token_missing_access_token_raises(monkeypatch):
    monkeypatch.setattr(sso_auth.urllib.request, "urlopen", lambda req, timeout=10: _FakeResp(b"{}"))
    monkeypatch.setattr(sso_auth, "sso_issuer", lambda: "https://sso.example.com")
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "")
    with pytest.raises(SsoAuthError):
        sso_auth.exchange_token("subject-abc", "market")


def test_exchange_token_requires_subject_and_audience(monkeypatch):
    monkeypatch.setattr(sso_auth, "sso_issuer", lambda: "https://sso.example.com")
    with pytest.raises(SsoAuthError):
        sso_auth.exchange_token("", "market")
    with pytest.raises(SsoAuthError):
        sso_auth.exchange_token("subject-abc", "")


# ---- 下游受众(内部用户 token 统一 gateway) ----


def test_sso_downstream_audience_defaults_to_gateway(monkeypatch):
    """受众统一: 未配置 env/config 时默认 gateway(market/rag/网关资源轨均接受)。"""
    monkeypatch.delenv("SSO_DOWNSTREAM_AUDIENCE", raising=False)
    assert sso_auth.sso_downstream_audience() == "gateway"


def test_sso_downstream_audience_env_override(monkeypatch):
    monkeypatch.setenv("SSO_DOWNSTREAM_AUDIENCE", "gateway-canary")
    assert sso_auth.sso_downstream_audience() == "gateway-canary"


# ---- 客户端覆盖(桌面 dashboard-gateway 等 public 客户端) ----


def test_exchange_token_client_override_public_client_no_secret(monkeypatch):
    """显式 client_id + 空 secret(public 客户端): 表单只带 client_id, 无 secret/Basic。"""
    captured: dict = {}

    def fake_urlopen(req, timeout=10):
        captured["data"] = req.data
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp(b'{"access_token":"downstream-token"}')

    monkeypatch.setattr(sso_auth.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sso_auth, "sso_issuer", lambda: "https://sso.example.com")
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "s3cr3t")

    out = sso_auth.exchange_token(
        "subject-abc", "gateway", client_id="dashboard-gateway", client_secret=""
    )
    assert out == "downstream-token"
    body = captured["data"].decode("utf-8")
    assert "client_id=dashboard-gateway" in body
    assert "client_secret" not in body
    assert captured["auth"] is None


def test_refresh_token_grant_default_keeps_agent_secret(monkeypatch):
    """默认路径零漂移: refresh 仍用 agent + secret(表单与 Basic 双注入)。"""
    captured: dict = {}

    def fake_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp(b'{"id_token":"new-id","refresh_token":"rt-2"}')

    monkeypatch.setattr(sso_auth.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sso_auth, "sso_issuer", lambda: "https://sso.example.com")
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "s3cr3t")

    out = sso_auth.refresh_token_grant("rt-1")
    assert out["id_token"] == "new-id"
    body = captured["data"].decode("utf-8")
    assert "grant_type=refresh_token" in body
    assert "refresh_token=rt-1" in body
    assert "client_id=agent" in body
    assert "client_secret=s3cr3t" in body
    assert captured["url"] == "https://sso.example.com/token"
    basic = captured["auth"]
    assert basic.startswith("Basic ")
    assert base64.b64decode(basic[6:]).decode("utf-8") == "agent:s3cr3t"


def test_refresh_token_grant_client_override_public_client_no_secret(monkeypatch):
    """显式 client_id + 空 secret: 表单只带 client_id, 无 secret/Basic。"""
    captured: dict = {}

    def fake_urlopen(req, timeout=10):
        captured["data"] = req.data
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp(b'{"id_token":"new-id"}')

    monkeypatch.setattr(sso_auth.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sso_auth, "sso_issuer", lambda: "https://sso.example.com")
    monkeypatch.setattr(sso_auth, "sso_client_id", lambda: "agent")
    monkeypatch.setattr(sso_auth, "sso_client_secret", lambda: "s3cr3t")

    out = sso_auth.refresh_token_grant(
        "rt-1", client_id="dashboard-gateway", client_secret=""
    )
    assert out["id_token"] == "new-id"
    body = captured["data"].decode("utf-8")
    assert "client_id=dashboard-gateway" in body
    assert "client_secret" not in body
    assert captured["auth"] is None
