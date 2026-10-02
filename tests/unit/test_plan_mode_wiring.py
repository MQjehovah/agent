"""Plan Mode 接线测试 — runner._maybe_apply_plan"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.runner import _maybe_apply_plan  # noqa: E402


class _Agent:
    def __init__(self, enabled=False, require_approval=False, workspace="", on_confirm=None):
        self._enable_plan_mode = enabled
        self._plan_mode = None
        self._plan_mode_config = {"auto_plan": True, "require_approval": require_approval}
        self.client = None
        self.workspace = workspace
        self.on_confirm = on_confirm


@pytest.mark.asyncio
async def test_disabled_returns_task_unchanged(tmp_path):
    agent = _Agent(enabled=False)
    task = "重构 src/main.py 的登录模块"
    assert await _maybe_apply_plan(agent, task) == task


@pytest.mark.asyncio
async def test_simple_task_not_planned(tmp_path):
    agent = _Agent(enabled=True)
    task = "你好"
    assert await _maybe_apply_plan(agent, task) == task


@pytest.mark.asyncio
async def test_enabled_rule_based_appends_plan(tmp_path):
    agent = _Agent(enabled=True, require_approval=False, workspace=str(tmp_path))
    out = await _maybe_apply_plan(agent, "重构 src/main.py 的登录模块")
    assert "已批准的执行计划" in out
    assert "重构 src/main.py 的登录模块" in out


@pytest.mark.asyncio
async def test_rejected_plan_returns_task_unchanged(tmp_path):
    async def deny(_name, _args):
        return False

    agent = _Agent(enabled=True, require_approval=True, workspace=str(tmp_path), on_confirm=deny)
    task = "重构 src/main.py 的登录模块"
    assert await _maybe_apply_plan(agent, task) == task
