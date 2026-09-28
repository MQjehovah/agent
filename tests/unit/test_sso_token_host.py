"""桌面 SSO token 托管接口 /api/auth/sso-tokens。

覆盖:
- 登录用户(JWT)托管后 sso_tokens 表出现该 uid 记录(id_token/refresh_token);
- 未登录 401、缺 id_token 400;
- 不同用户各存各的(隔离), body 内 uid 被忽略(仅允许写本人)。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import storage.storage as storage_mod  # noqa: E402
from storage.storage import Storage  # noqa: E402
from web.server import WebServer, create_jwt  # noqa: E402


@pytest.fixture(autouse=True)
def _auth_enabled(monkeypatch):
    """确保走真实鉴权路径(其它测试文件可能置 1; 模块导入期改 env 会泄漏给后续测试)。"""
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")


@pytest.fixture
def env(tmp_path):
    """临时 Storage 注入全局单例 + WebServer/TestClient。"""
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    w = WebServer()
    client = TestClient(w._app)
    yield s, client
    s.close()
    storage_mod._storage_instance = prev


def _auth(uid: int = 7, name: str = "张三", role: str = "default") -> dict:
    return {"Authorization": f"Bearer {create_jwt({'id': uid, 'name': name, 'role': role})}"}


def _row(storage: Storage, uid: int) -> dict | None:
    with storage.get_connection() as conn:
        r = conn.execute(
            "SELECT id_token, refresh_token FROM sso_tokens WHERE user_id=?", (uid,)
        ).fetchone()
    return dict(r) if r else None


def test_host_tokens_saves_current_user_tokens(env):
    s, client = env
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": "id-tok-7", "refresh_token": "ref-7"},
        headers=_auth(7),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}
    assert _row(s, 7) == {"id_token": "id-tok-7", "refresh_token": "ref-7"}


def test_host_tokens_overwrite_is_idempotent(env):
    """续期复投: 同一用户重复托管覆盖旧值。"""
    s, client = env
    client.post(
        "/api/auth/sso-tokens",
        json={"id_token": "id-old", "refresh_token": "ref-old"},
        headers=_auth(7),
    )
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": "id-new", "refresh_token": "ref-new"},
        headers=_auth(7),
    )
    assert resp.status_code == 200, resp.text
    assert _row(s, 7) == {"id_token": "id-new", "refresh_token": "ref-new"}


def test_host_tokens_requires_auth(env):
    s, client = env
    resp = client.post(
        "/api/auth/sso-tokens", json={"id_token": "id-tok", "refresh_token": "ref"}
    )
    assert resp.status_code == 401
    assert _row(s, 7) is None


@pytest.mark.parametrize(
    "body", [{}, {"id_token": ""}, {"id_token": "   ", "refresh_token": "r"}]
)
def test_host_tokens_requires_id_token(env, body):
    s, client = env
    resp = client.post("/api/auth/sso-tokens", json=body, headers=_auth(7))
    assert resp.status_code == 400, resp.text
    assert _row(s, 7) is None


def test_host_tokens_isolated_between_users(env):
    s, client = env
    r7 = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": "id-7", "refresh_token": "ref-7"},
        headers=_auth(7),
    )
    r8 = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": "id-8", "refresh_token": "ref-8"},
        headers=_auth(8, name="李四"),
    )
    assert (r7.status_code, r8.status_code) == (200, 200)
    assert _row(s, 7) == {"id_token": "id-7", "refresh_token": "ref-7"}
    assert _row(s, 8) == {"id_token": "id-8", "refresh_token": "ref-8"}


def test_host_tokens_ignores_body_uid(env):
    """uid 取自登录态, body 里的 uid 不生效(防越权代写)。"""
    s, client = env
    resp = client.post(
        "/api/auth/sso-tokens",
        json={"id_token": "id-7", "refresh_token": "ref-7", "uid": 9},
        headers=_auth(7),
    )
    assert resp.status_code == 200, resp.text
    assert _row(s, 7) == {"id_token": "id-7", "refresh_token": "ref-7"}
    assert _row(s, 9) is None
