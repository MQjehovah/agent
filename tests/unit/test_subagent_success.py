"""P0 回归测试：个人子代理成功执行后不应因 time 未导入而报 NameError。

根因：agent.py 模块级曾缺失 `import time`（仅在 _get_env_context 局部导入），
导致 _execute_subagent 在 `instance.last_used = time.time()` 处抛 NameError，
被 except 吞掉后对外返回「子代理执行错误」——子代理实际已跑完，结果却报失败。
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent import Agent, AgentResult, RunContext, _current_run  # noqa: E402
from hooks import HookManager  # noqa: E402


def _make_agent():
    """构造最小可用 Agent（_execute_subagent 只需 hooks/rbac/subagent_manager）。"""
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

    # task_count / last_used 必须是真实数值（_execute_subagent 会 += 1 / = time.time()）
    instance = SimpleNamespace(
        agent=sub_agent, task_count=0, last_used=0.0, session_id="sess-sub",
    )

    sm = MagicMock()
    sm.is_team.return_value = False  # 关键：走个人子代理路径，而非团队编排
    sm.get_or_create_subagent = AsyncMock(return_value=(instance, True))
    sm.get_stats.return_value = {"active_count": 1}
    return sm, instance


@pytest.mark.asyncio
async def test_personal_subagent_success_returns_success():
    """子代理成功执行后，对外应返回 success=True，而非被 NameError 误判为失败。"""
    agent = _make_agent()
    sm, instance = _mock_personal_subagent()
    agent.subagent_manager = sm

    # _execute_subagent 读取 current_run().session.role 做 RBAC 判断
    ctx = RunContext(task="修复截图")
    ctx.session = MagicMock()
    ctx.session.role = "admin"
    token = _current_run.set(ctx)
    try:
        raw = await agent._execute_subagent({
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
    # 真正的回归断言：last_used 被 time.time() 正常更新（无 NameError）
    assert instance.last_used > 0
    assert instance.task_count == 1
