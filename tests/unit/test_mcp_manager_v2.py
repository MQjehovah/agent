"""MCP Python SDK v2 客户端(manager)行为测试。

覆盖 v2 关键变更:
- 工具定义字段 snake_case: Tool.input_schema → tool_defs.parameters;
- CallToolResult 文本拼接、structured_content 兜底 JSON(ensure_ascii=False)、is_error 文案;
- MCPError(CONNECTION_CLOSED/REQUEST_TIMEOUT) 置断连, 其它 code 保持连接;
- 关闭清理在调用方被取消时仍能在同任务内续跑剩余 ExitStack 回调。
"""
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from mcp import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

import mcps.manager as manager


def _make_connection(result=None, error=None):
    """构造已连接的 MCPServerConnection, session.call_tool 返回 result 或抛 error。"""

    async def _call_tool(name, args):
        if error is not None:
            raise error
        return result

    conn = manager.MCPServerConnection("demo", {})
    conn.session = SimpleNamespace(call_tool=_call_tool)
    conn._connected = True
    return conn


async def test_connect_maps_input_schema_to_parameters(monkeypatch):
    schema = {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}
    tool = SimpleNamespace(name="query", description="执行查询", input_schema=schema)
    fake_session = SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[tool])),
    )

    class FakeSessionCtx:
        def __init__(self, read_stream, write_stream):
            pass

        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *exc_info):
            return False

    @asynccontextmanager
    async def fake_stdio_client(server, errlog=None):
        yield (object(), object())

    monkeypatch.setattr(manager, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(manager, "ClientSession", FakeSessionCtx)

    conn = manager.MCPServerConnection("demo", {"command": "python", "args": []})
    assert await conn.connect() is True
    assert conn.tool_defs == [{
        "type": "function",
        "function": {"name": "query", "description": "执行查询", "parameters": schema},
    }]

    await conn.close()
    assert conn.is_connected is False


async def test_call_tool_joins_text_content():
    result = SimpleNamespace(content=[SimpleNamespace(text="第一行"), SimpleNamespace(text="第二行"), "raw"])
    assert await _make_connection(result=result).call_tool("echo", {}) == "第一行\n第二行\nraw"


async def test_call_tool_structured_content_fallback_json():
    result = SimpleNamespace(content=[], structured_content={"名称": "设备A", "ok": True}, is_error=False)
    out = await _make_connection(result=result).call_tool("query", {})
    assert json.loads(out) == {"名称": "设备A", "ok": True}
    assert "设备A" in out


async def test_call_tool_empty_result_reports_is_error():
    failed = SimpleNamespace(content=[], structured_content=None, is_error=True)
    ok = SimpleNamespace(content=[], structured_content=None, is_error=False)
    assert await _make_connection(result=failed).call_tool("t", {}) == "执行失败"
    assert await _make_connection(result=ok).call_tool("t", {}) == "执行成功"


async def test_call_tool_tolerates_result_without_fields():
    assert await _make_connection(result=SimpleNamespace()).call_tool("t", {}) == "执行成功"


async def test_call_tool_marks_disconnected_on_connection_closed():
    conn = _make_connection(error=MCPError(CONNECTION_CLOSED, "connection closed"))
    out = await conn.call_tool("query", {})
    assert out.startswith("执行失败")
    assert conn._connected is False
    assert await conn.call_tool("query", {}) == "MCP未连接"


async def test_call_tool_marks_disconnected_on_request_timeout():
    conn = _make_connection(error=MCPError(REQUEST_TIMEOUT, "request timed out"))
    assert (await conn.call_tool("query", {})).startswith("执行失败")
    assert conn._connected is False


async def test_call_tool_keeps_connection_on_other_mcp_error():
    conn = _make_connection(error=MCPError(-32602, "invalid params"))
    assert (await conn.call_tool("query", {})).startswith("执行失败")
    assert conn._connected is True


async def test_call_tool_marks_disconnected_on_os_error():
    conn = _make_connection(error=BrokenPipeError("broken pipe"))
    assert (await conn.call_tool("query", {})).startswith("执行失败")
    assert conn._connected is False


async def test_cleanup_finishes_remaining_unwind_after_cancellation():
    events = []

    @asynccontextmanager
    async def plain_resource():
        try:
            yield
        finally:
            events.append("plain-closed")

    @asynccontextmanager
    async def cancellable_resource():
        try:
            yield
        finally:
            events.append("cancellable-entered")
            await asyncio.sleep(0.1)
            events.append("cancellable-closed")

    conn = manager.MCPServerConnection("demo", {})
    stack = AsyncExitStack()
    await stack.enter_async_context(plain_resource())
    await stack.enter_async_context(cancellable_resource())
    conn._exit_stack = stack
    conn._connected = True

    task = asyncio.create_task(conn._safe_exit_stack_cleanup())
    await asyncio.sleep(0.02)
    task.cancel()
    await task

    assert events == ["cancellable-entered", "plain-closed"]
    assert conn._exit_stack is None
    assert conn._connected is False
