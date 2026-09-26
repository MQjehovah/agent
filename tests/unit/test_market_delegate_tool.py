"""专家委派工具(轻量无状态/OBO)单测。

覆盖:
- 拉专家人设(persona) + 依赖并注入技能文本, 起临时子代理执行;
- 依赖含 local mcp → 拒绝;
- 无 subject fail-closed; 市场未配置返回错误。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.core import RunContext, _current_run  # noqa: E402
from tools import market_delegate as md  # noqa: E402
from tools.market_delegate import MarketDelegateTool  # noqa: E402


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    deps: list = []
    posts: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kwargs):
        return _FakeResp(200, {"agent": "e", "prompt": "你是报销专家", "dependencies": list(_FakeClient.deps)})

    async def post(self, url, headers=None, json=None, **kwargs):
        _FakeClient.posts.append(url)
        if "/runtime/mcp/" in url and url.endswith("/connect"):
            return _FakeResp(200, {"tools": [{"name": "run_command"}, {"name": "open_session"}]})
        return _FakeResp(200, {"result": {"skill_md": "# 报销流程\n先填单再审批"}})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("MARKET_BASE_URL", "http://market.local")
    monkeypatch.setenv("MARKET_SERVICE_TOKEN", "svc-token")
    _FakeClient.deps = []
    _FakeClient.posts = []
    md._SKILL_CACHE.clear()
    yield


async def _run_with(user_id: str, coro_factory):
    rc = RunContext(user_id=user_id, role="default")
    token = _current_run.set(rc)
    try:
        return await coro_factory()
    finally:
        _current_run.reset(token)


def test_schema_object():
    p = MarketDelegateTool().parameters
    assert p.get("type") == "object"
    assert set(p.get("required") or []) == {"expert", "task"}


async def test_happy_path_composes_and_runs(monkeypatch):
    monkeypatch.setattr(md.httpx, "AsyncClient", _FakeClient)
    _FakeClient.deps = [
        {"name": "reimburse", "type": "skill", "distribution": "remote"},
        {"name": "gitlab-devops", "type": "mcp", "distribution": "remote"},
    ]
    captured: dict = {}

    async def fake_run(system_prompt, task):
        captured["system_prompt"] = system_prompt
        captured["task"] = task
        return {"ok": True, "expert_output": "已处理", "error": ""}

    monkeypatch.setattr(md, "_run_transient", fake_run)

    out = await _run_with("web:jimingqing", lambda: MarketDelegateTool().execute(expert="报销专家", task="帮我报销差旅"))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["expert_output"] == "已处理"
    assert payload["expert"] == "报销专家"
    # 技能文本被注入
    assert "报销流程" in captured["system_prompt"]
    assert "你是报销专家" in captured["system_prompt"]
    assert "gitlab-devops" in captured["system_prompt"]
    assert "market_runtime" in captured["system_prompt"]
    assert "run_command" in captured["system_prompt"]  # mcp 工具清单已列出
    assert captured["task"] == "帮我报销差旅"
    # 技能激活被调用
    assert any("/api/runtime/skills/reimburse/activate" in u for u in _FakeClient.posts)


async def test_rejects_local_mcp_dep(monkeypatch):
    monkeypatch.setattr(md.httpx, "AsyncClient", _FakeClient)
    _FakeClient.deps = [{"name": "gitlab-devops", "type": "mcp", "distribution": "local"}]
    called = {"run": False}

    async def fake_run(system_prompt, task):
        called["run"] = True
        return {"ok": True}

    monkeypatch.setattr(md, "_run_transient", fake_run)
    out = await _run_with("web:jimingqing", lambda: MarketDelegateTool().execute(expert="e", task="t"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "本地(stdio)" in payload["error"]
    assert called["run"] is False


async def test_requires_subject(monkeypatch):
    monkeypatch.setattr(md.httpx, "AsyncClient", _FakeClient)
    out = await _run_with("", lambda: MarketDelegateTool().execute(expert="e", task="t"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "市场身份" in payload["error"]


async def test_disabled_without_config(monkeypatch):
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    out = await _run_with("web:jimingqing", lambda: MarketDelegateTool().execute(expert="e", task="t"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "未配置" in payload["error"]
