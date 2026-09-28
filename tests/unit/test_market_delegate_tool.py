"""专家委派工具(轻量无状态/OBO)单测。

覆盖:
- 拉专家人设(persona) + 依赖并注入技能文本, 起临时子代理执行;
- 逐请求携带用户 token(Bearer, aud=gateway), 不再带服务令牌 / X-Act-As-Sub;
- 依赖含 local mcp → 拒绝;
- uid 解析失败 / 无托管 token fail-closed 统一引导登录; 市场未配置返回错误。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.core import RunContext, _current_run  # noqa: E402
from tools import market_delegate as md  # noqa: E402
from tools.market_delegate import MarketDelegateTool  # noqa: E402
from web.sso_tokens import USER_TOKEN_HINT as _HINT  # noqa: E402


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
    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, **kwargs):
        _FakeClient.captured = {"url": url, "headers": headers}
        # 与市场 /api/runtime/agents/{name}/persona 真实响应一致: 内容包在 result 里
        return _FakeResp(200, {
            "ok": True,
            "capability": {"name": "e"},
            "action": "persona",
            "message": "ok",
            "result": {"agent": "e", "prompt": "你是报销专家", "dependencies": list(_FakeClient.deps)},
        })

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
    _FakeClient.captured = {}
    md._SKILL_CACHE.clear()
    yield


@pytest.fixture
def hosted_token(monkeypatch):
    """托管用户 token(按 uid 派生), 供断言 Bearer 头。"""
    from web import sso_tokens

    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: f"user-tok-{uid}")
    return "user-tok-7"


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


async def test_happy_path_composes_and_runs(monkeypatch, hosted_token):
    monkeypatch.setattr(md.httpx, "AsyncClient", _FakeClient)
    _FakeClient.deps = [
        {"name": "reimburse", "type": "skill", "distribution": "remote"},
        {"name": "gitlab-devops", "type": "mcp", "distribution": "remote"},
    ]
    captured: dict = {}

    async def fake_run(system_prompt, task, expert=""):
        captured["system_prompt"] = system_prompt
        captured["task"] = task
        return {"ok": True, "expert_output": "已处理", "error": ""}

    monkeypatch.setattr(md, "_run_transient", fake_run)

    out = await _run_with("web:7", lambda: MarketDelegateTool().execute(expert="报销专家", task="帮我报销差旅"))
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["expert_output"] == "已处理"
    assert payload["expert"] == "报销专家"
    # persona 请求逐请求携带用户 token, 不带服务令牌 / X-Act-As-Sub
    cap = _FakeClient.captured
    assert cap["url"] == "http://market.local/api/runtime/agents/报销专家/persona"
    assert cap["headers"]["Authorization"] == "Bearer user-tok-7"
    assert "X-Act-As-Sub" not in cap["headers"]
    # 技能文本被注入
    assert "报销流程" in captured["system_prompt"]
    assert "你是报销专家" in captured["system_prompt"]
    assert "gitlab-devops" in captured["system_prompt"]
    assert "market_runtime" in captured["system_prompt"]
    assert "run_command" in captured["system_prompt"]  # mcp 工具清单已列出
    assert captured["task"] == "帮我报销差旅"
    # 技能激活被调用
    assert any("/api/runtime/skills/reimburse/activate" in u for u in _FakeClient.posts)


async def test_rejects_local_mcp_dep(monkeypatch, hosted_token):
    monkeypatch.setattr(md.httpx, "AsyncClient", _FakeClient)
    _FakeClient.deps = [{"name": "gitlab-devops", "type": "mcp", "distribution": "local"}]
    called = {"run": False}

    async def fake_run(system_prompt, task, expert=""):
        called["run"] = True
        return {"ok": True}

    monkeypatch.setattr(md, "_run_transient", fake_run)
    out = await _run_with("web:7", lambda: MarketDelegateTool().execute(expert="e", task="t"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "本地(stdio)" in payload["error"]
    assert called["run"] is False


async def test_fail_closed_without_user_token(monkeypatch):
    """uid 解析失败(空/中文旧账号名/web:0) → fail-closed 统一引导登录, 不发市场请求。"""
    monkeypatch.setattr(md.httpx, "AsyncClient", _FakeClient)
    for user_id in ("", "web:朱尚荣", "web:0"):
        out = await _run_with(user_id, lambda: MarketDelegateTool().execute(expert="e", task="t"))
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_requires_hosted_token(monkeypatch):
    from web import sso_tokens

    monkeypatch.setattr(md.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(sso_tokens, "get_downstream_token",
                        lambda uid, audience="", **kwargs: "")
    out = await _run_with("web:7", lambda: MarketDelegateTool().execute(expert="e", task="t"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == _HINT
    assert _FakeClient.captured == {}


async def test_disabled_without_config(monkeypatch):
    monkeypatch.delenv("MARKET_BASE_URL", raising=False)
    monkeypatch.delenv("MARKET_SERVICE_TOKEN", raising=False)
    out = await _run_with("web:7", lambda: MarketDelegateTool().execute(expert="e", task="t"))
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "未配置" in payload["error"]
    assert _FakeClient.captured == {}


async def test_market_delegate_run_on_main_loop_flag():
    """market_delegate 必须标记主循环内联执行: 其访问的子代理管理器 asyncio.Lock
    绑定主循环, 经工具线程池(独立临时 loop)执行会抛 'bound to a different event loop'。"""
    tool = MarketDelegateTool()
    assert tool.run_on_main_loop is True


async def test_registry_inlines_main_loop_tools_only():
    """ToolRegistry: 标记 run_on_main_loop 的工具在主循环执行, 其余仍走线程池。"""
    import asyncio

    from tools import BuiltinTool, ToolRegistry

    class _Probe(BuiltinTool):
        def __init__(self, probe_name: str, inline: bool):
            self._probe_name = probe_name
            self.run_on_main_loop = inline

        @property
        def name(self) -> str:
            return self._probe_name

        @property
        def description(self) -> str:
            return "loop probe"

        @property
        def parameters(self) -> dict:
            return {}

        async def execute(self, **kwargs) -> str:
            return str(id(asyncio.get_running_loop()))

    reg = ToolRegistry()
    reg.register_tool(_Probe("probe_inline", True))
    reg.register_tool(_Probe("probe_worker", False))
    outer = str(id(asyncio.get_running_loop()))
    assert await reg.execute("probe_inline", {}) == outer
    assert await reg.execute("probe_worker", {}) != outer
