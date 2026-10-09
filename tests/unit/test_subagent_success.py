"""个人子代理执行路径回归：execute_subagent 成功时应返回 success=True。

背景：早期 `Agent._execute_subagent` 曾因模块级缺失 `import time` 被 except 吞掉、
对外误报「子代理执行错误」。该入口现已迁移到 `agent.executor.execute_subagent`，
本用例锁定新入口的成功路径与返回信封。
"""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.core import Agent, AgentResult, RunContext, _current_run  # noqa: E402
from agent.executor import execute_subagent  # noqa: E402
from hooks import HookManager  # noqa: E402


def _make_agent():
    """构造最小可用 Agent（execute_subagent 只需 hooks/rbac/subagent_manager）。"""
    agent = Agent(workspace=".", client=MagicMock())
    agent.name = "测试主代理"
    agent.agent_id = "test"
    agent.rbac = None  # 跳过 RBAC 校验
    agent.session_manager = None
    agent.hooks = HookManager()  # 真实 HookManager，fire 不报错
    return agent


def _mock_personal_subagent():
    """构造一个「个人子代理」（is_team=False 路径）所需的 mock 实例。"""
    sub_agent = MagicMock()
    sub_agent.hooks.register = MagicMock()
    sub_agent.hooks.unregister = MagicMock()
    sub_agent.run = AsyncMock(return_value=AgentResult(
        agent_id="sub", status="completed", result="子代理已完成任务",
    ))

    instance = SimpleNamespace(
        agent=sub_agent, task_count=0, last_used=0.0, session_id="sess-sub",
        run_lock=asyncio.Lock(),
    )

    sm = MagicMock()
    sm.is_team.return_value = False  # 关键：走个人子代理路径，而非团队编排
    sm.get_or_create_subagent = AsyncMock(return_value=(instance, True))
    sm.cleanup_subagent = AsyncMock()
    sm.run_semaphore = MagicMock(return_value=None)
    sm.get_stats.return_value = {"active_count": 1}
    return sm, instance


@pytest.mark.asyncio
async def test_personal_subagent_success_returns_success():
    """个人子代理成功执行后，对外应返回 success=True（不得被异常吞成失败）。"""
    agent = _make_agent()
    sm, instance = _mock_personal_subagent()
    agent.subagent_manager = sm

    # execute_subagent 读取 current_run().session.role 做 RBAC 判断
    ctx = RunContext(task="修复截图")
    ctx.session = MagicMock()
    ctx.session.role = "admin"
    token = _current_run.set(ctx)
    try:
        raw = await execute_subagent(agent, {
            "task": "把截图双屏 Bug 修掉",
            "name": "截图修复",
            "template": "代码审查",
            "session_id": "sess-sub",
        })
    finally:
        _current_run.reset(token)

    parsed = json.loads(raw)
    assert parsed["success"] is True, f"子代理被误判失败: {parsed}"
    assert parsed["status"] == "completed"
    assert "子代理已完成任务" in parsed["result"]
