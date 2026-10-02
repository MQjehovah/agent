"""P0-1 验证：工具执行后的插件后处理钩子被接线（on_post_tool_call / on_transform_tool_result）。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.executor import _apply_post_tool_hooks, _maybe_append_diagnostics  # noqa: E402
from hooks import HookEvent, HookManager  # noqa: E402


class _Plugin:
    def __init__(self, name="p", post=None, transform=None):
        self.name = name
        self.enabled = True
        self._post = post
        self._transform = transform

    async def on_post_tool_call(self, tool_name, args, result):
        if self._post is None:
            return result
        return self._post(result)

    async def on_transform_tool_result(self, tool_name, result):
        if self._transform is None:
            return result
        return self._transform(result)


class _PM:
    def __init__(self, plugins):
        self.plugins = {p.name: p for p in plugins}


class _Agent:
    def __init__(self, plugins):
        self.plugin_manager = _PM(plugins)
        self.hooks = HookManager()
        self._hook_event = HookEvent


@pytest.mark.asyncio
async def test_post_then_transform_order():
    agent = _Agent([_Plugin(post=lambda r: r + "|P", transform=lambda r: r.upper())])
    out = await _apply_post_tool_hooks(agent, "edit", {}, "abc")
    assert out == "ABC|P"


@pytest.mark.asyncio
async def test_plugin_exception_does_not_break():
    def boom(_r):
        raise RuntimeError("boom")

    agent = _Agent([_Plugin(post=boom, transform=lambda r: r)])
    out = await _apply_post_tool_hooks(agent, "edit", {}, "abc")
    assert out == "abc"


@pytest.mark.asyncio
async def test_non_string_return_is_json_serialized():
    agent = _Agent([_Plugin(post=lambda _r: {"success": True, "n": 1})])
    out = await _apply_post_tool_hooks(agent, "edit", {}, "abc")
    assert '"n": 1' in out


@pytest.mark.asyncio
async def test_post_tool_use_event_fired():
    agent = _Agent([])
    seen = []

    async def on_post(ctx):
        seen.append((ctx.tool_name, ctx.result))

    agent.hooks.register(HookEvent.POST_TOOL_USE, on_post)
    await _apply_post_tool_hooks(agent, "git", {}, "ok")
    assert seen == [("git", "ok")]


@pytest.mark.asyncio
async def test_disabled_plugin_skipped():
    p = _Plugin(post=lambda r: r + "|X")
    p.enabled = False
    agent = _Agent([p])
    out = await _apply_post_tool_hooks(agent, "edit", {}, "abc")
    assert out == "abc"


class _DiagAgent:
    def __init__(self, workspace):
        self.workspace = workspace


@pytest.mark.asyncio
async def test_post_diagnostics_modes(tmp_path, monkeypatch):
    import importlib.util
    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff not installed")
    (tmp_path / "bad.py").write_text("import os\n", encoding="utf-8")
    agent = _DiagAgent(str(tmp_path))
    result = json.dumps({"success": True, "path": "bad.py"})

    monkeypatch.setenv("AGENT_POST_EDIT_DIAGNOSTICS", "off")
    assert await _maybe_append_diagnostics(agent, "edit", {"path": "bad.py"}, result) == result

    monkeypatch.setenv("AGENT_POST_EDIT_DIAGNOSTICS", "advisory")
    out = await _maybe_append_diagnostics(agent, "edit", {"path": "bad.py"}, result)
    assert "代码诊断" in out

    monkeypatch.setenv("AGENT_POST_EDIT_DIAGNOSTICS", "block")
    out = await _maybe_append_diagnostics(agent, "edit", {"path": "bad.py"}, result)
    data = json.loads(out)
    assert data["success"] is False and "diagnostics" in data
