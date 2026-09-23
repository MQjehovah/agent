"""工具执行错误可读性 + 取消语义回归(ToolRegistry / execute_tool_safe)。

线上问题背景: ask_user 在 180s 工具超时后以「工具 'ask_user' 执行失败: 」空文案
收尾 —— asyncio.TimeoutError 的 str() 为空(且 CancelledError 不属于 Exception,
不会被通用 except 捕获)。本文件锁定:
- 空消息异常 → 错误文案回退异常类名, 不再出现空文案;
- CancelledError 在工具执行链上不被吞, 向上传播并记录 warning。
"""
import asyncio
import json
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.executor import execute_tool_safe  # noqa: E402
from tools import BuiltinTool, ToolRegistry  # noqa: E402

EXEC_LOGGER = "agent.agent"


class _ToolBase(BuiltinTool):
    async def execute(self, **kwargs) -> str:
        raise NotImplementedError


class _BoomTool(_ToolBase):
    @property
    def name(self) -> str:
        return "boom"

    @property
    def description(self) -> str:
        return "测试: 抛空消息 RuntimeError"

    @property
    def parameters(self) -> dict:
        return {}

    async def execute(self, **kwargs) -> str:
        raise RuntimeError()


class _TimeoutTool(_ToolBase):
    @property
    def name(self) -> str:
        return "timeout_tool"

    @property
    def description(self) -> str:
        return "测试: 抛 str() 为空的 TimeoutError"

    @property
    def parameters(self) -> dict:
        return {}

    async def execute(self, **kwargs) -> str:
        raise asyncio.TimeoutError()


class _CancelTool(_ToolBase):
    @property
    def name(self) -> str:
        return "cancel_tool"

    @property
    def description(self) -> str:
        return "测试: 抛 CancelledError"

    @property
    def parameters(self) -> dict:
        return {}

    async def execute(self, **kwargs) -> str:
        raise asyncio.CancelledError()


def _make_agent(tool_side_effect):
    """最小 agent 替身: 走 execute_tool_safe → execute_tool → tool_registry.execute 链路。"""
    agent = MagicMock()
    agent.permission.check.return_value = SimpleNamespace(reason="")
    agent.rbac = None
    agent.on_confirm = None
    agent.sandbox = None
    agent.plugin_manager = None
    agent._circuit_breaker = None
    agent.hooks.fire = AsyncMock()
    agent.tracer = MagicMock()
    agent.tool_registry.has_tool.return_value = True
    agent.tool_registry.execute = AsyncMock(side_effect=tool_side_effect)
    agent.mcp = None
    agent.skill_manager = None
    agent.subagent_manager = None
    return agent


# ---------------- ToolRegistry.execute: 空文案回退 / 取消传播 ----------------


async def test_registry_empty_message_error_text_contains_class_name():
    reg = ToolRegistry()
    reg.register_tool(_BoomTool())
    result = await reg.execute("boom", {})
    assert result == "错误: 工具 'boom' 执行失败 - RuntimeError"


async def test_registry_empty_timeout_error_text_contains_class_name():
    """TimeoutError 的 str() 为空 → 回退类名(线上空文案的直接来源)。"""
    reg = ToolRegistry()
    reg.register_tool(_TimeoutTool())
    result = await reg.execute("timeout_tool", {})
    assert result == "错误: 工具 'timeout_tool' 执行失败 - TimeoutError"


async def test_registry_cancelled_error_propagates():
    reg = ToolRegistry()
    reg.register_tool(_CancelTool())
    with pytest.raises(asyncio.CancelledError):
        await reg.execute("cancel_tool", {})


# ---------------- execute_tool_safe: 空文案回退 / 取消传播 ----------------


async def test_execute_tool_safe_empty_error_uses_class_name():
    """execute_tool 抛空消息异常 → 结果文案含异常类名, 不再是空字符串。"""
    agent = _make_agent(RuntimeError())
    result = await execute_tool_safe(agent, "boom", {})
    assert result == "工具执行错误: RuntimeError"


async def test_execute_tool_safe_own_handler_uses_class_name():
    """execute_tool_safe 自身通用 handler: 空消息异常 → JSON 错误文案回退类名。"""
    agent = _make_agent(None)
    agent.tool_registry.execute = AsyncMock(return_value="ok")
    agent._hook_event = SimpleNamespace(PRE_TOOL_USE="pre", TOOL_START="start", TOOL_RESULT="result")

    async def _fire(event, **kwargs):
        if event == "result":
            raise asyncio.TimeoutError()

    agent.hooks.fire = AsyncMock(side_effect=_fire)
    result = await execute_tool_safe(agent, "boom", {})
    assert json.loads(result) == {"success": False, "error": "TimeoutError"}


async def test_execute_tool_safe_cancel_re_raises_and_logs(caplog):
    agent = _make_agent(asyncio.CancelledError())
    with caplog.at_level(logging.WARNING, logger=EXEC_LOGGER), pytest.raises(asyncio.CancelledError):
        await execute_tool_safe(agent, "boom", {})
    assert any("工具调用被取消: boom" in rec.getMessage() for rec in caplog.records)
