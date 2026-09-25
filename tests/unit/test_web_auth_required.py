"""未鉴权 /api/chat 与 /api/chat/stream 必须 401(去掉静默 anon 回落)。

覆盖:
- 无凭证: 两个端点均 401, 且不创建 web:anon: 会话(_get_or_create_session 未被调用);
- 合法 JWT: 正常进入(会话命名空间 web:{uid});
- X-Service-Token: 按 uid 0 服务账号进入;
- WEBUI_DISABLE_AUTH=1 语义仍由 _get_auth 处理(此处显式置 0 验证鉴权路径)。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from fastapi.testclient import TestClient

import storage.storage as storage_mod
from storage.storage import Storage
from web.server import WebServer, create_jwt

SERVICE_TOKEN = "test-svc-token"


class _FakeChatSession:
    def __init__(self):
        self.is_streaming = False
        self.stream_started_at = ""
        self.messages = []
        self.stage = ""

    def add_message(self, role, content):
        self.messages.append((role, content))

    def start_stream(self):
        self.is_streaming = True

    def stop_stream(self):
        self.is_streaming = False

    def set_stage(self, stage):
        self.stage = stage


class _FakeHooks:
    def register(self, *args, **kwargs):
        pass

    def unregister(self, *args, **kwargs):
        pass


class _FakeRouter:
    def __init__(self, agent):
        self.agent = agent

    @staticmethod
    def format_session_id(channel, uid, rand):
        return f"{channel}:{uid}:{rand}"

    async def route(self, *args, **kwargs):
        return "ok"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBUI_DISABLE_AUTH", "0")
    # 构造 WebServer 前注入服务令牌(_get_authz 的闭包在构造期读取)
    monkeypatch.setenv("AGENT_SERVICE_TOKEN", SERVICE_TOKEN)
    prev = storage_mod._storage_instance
    s = Storage(str(tmp_path))
    storage_mod._storage_instance = s
    w = WebServer()
    w.set_agent(SimpleNamespace())  # 占位: 通过 handler 的 agent 初始化检查
    created: list[str] = []

    def _fake_get_or_create(session_id):
        created.append(session_id)
        return _FakeChatSession()

    monkeypatch.setattr(w, "_get_or_create_session", _fake_get_or_create)
    client = TestClient(w._app)
    yield w, s, client, created
    s.close()
    storage_mod._storage_instance = prev


def _token(uid: int = 7, role: str = "default", name: str = "用户") -> dict:
    return {"Authorization": f"Bearer {create_jwt({'id': uid, 'name': name, 'role': role})}"}


@pytest.mark.parametrize("path", ["/api/chat", "/api/chat/stream"])
def test_chat_requires_auth(env, path):
    w, st, client, created = env
    r = client.post(path, json={"message": "你好"})
    assert r.status_code == 401, r.text
    assert created == []  # 不存在 web:anon: 会话创建路径


def test_chat_with_valid_jwt_enters(env, monkeypatch):
    w, st, client, created = env

    async def _no_llm(auth):
        raise RuntimeError("test: no llm")

    monkeypatch.setattr(w, "_agent_for_web", _no_llm)
    r = client.post("/api/chat", json={"message": "你好"}, headers=_token(7))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "processing"
    assert created and created[0].startswith("web:7:"), created


def test_chat_stream_with_valid_jwt_enters(env, monkeypatch):
    w, st, client, created = env
    monkeypatch.setattr("channels.MessageRouter", _FakeRouter, raising=False)

    async def _agent_for_web(auth):
        return SimpleNamespace(hooks=_FakeHooks(), _permission_config=None), ""

    monkeypatch.setattr(w, "_agent_for_web", _agent_for_web)
    r = client.post("/api/chat/stream", json={"message": "你好"}, headers=_token(7))
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers.get("content-type", "")
    assert created and created[0].startswith("web:7:"), created


def test_chat_with_service_token_uid0(env, monkeypatch):
    w, st, client, created = env

    async def _no_llm(auth):
        raise RuntimeError("test: no llm")

    monkeypatch.setattr(w, "_agent_for_web", _no_llm)
    r = client.post("/api/chat", json={"message": "你好"},
                    headers={"X-Service-Token": SERVICE_TOKEN})
    assert r.status_code == 200, r.text
    assert created and created[0].startswith("web:0:"), created


def test_chat_stream_service_token_uses_minimal_role(env, monkeypatch):
    """服务间凭证对话不隐式 admin: 运行角色为最小 'service'(工具全拒), 仍按 uid 0 进入。"""
    w, st, client, created = env
    captured: dict = {}

    class _CaptureRouter:
        def __init__(self, agent):
            self.agent = agent

        @staticmethod
        def format_session_id(channel, uid, rand):
            return f"{channel}:{uid}:{rand}"

        async def route(self, *args, **kwargs):
            captured.update(kwargs)
            return "ok"

    monkeypatch.setattr("channels.MessageRouter", _CaptureRouter, raising=False)

    async def _agent_for_web(auth):
        return SimpleNamespace(hooks=_FakeHooks(), _permission_config=None), ""

    monkeypatch.setattr(w, "_agent_for_web", _agent_for_web)
    r = client.post("/api/chat/stream", json={"message": "你好"},
                    headers={"X-Service-Token": SERVICE_TOKEN})
    assert r.status_code == 200, r.text
    assert captured.get("role") == "service", captured
    assert captured.get("user_role") == "service", captured
    assert created and created[0].startswith("web:0:"), created


def test_chat_invalid_token_still_401(env):
    w, st, client, created = env
    r = client.post("/api/chat", json={"message": "你好"},
                    headers={"Authorization": "Bearer bad.token.value"})
    assert r.status_code == 401, r.text
    assert created == []
