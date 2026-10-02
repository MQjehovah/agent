"""后台/流式终端工具：start/read/list/stop。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tools.terminal import TerminalTool, stop_all_sessions  # noqa: E402


@pytest.mark.asyncio
async def test_terminal_start_read_stop():
    tool = TerminalTool()
    # 跨平台: python -c 打印 123 后短暂存活
    cmd = f'"{sys.executable}" -c "import sys,time;print(123);sys.stdout.flush();time.sleep(5)"'
    try:
        r = await tool.execute(operation="start", command=cmd)
    except NotImplementedError:
        pytest.skip("当前事件循环不支持子进程(Windows Selector loop)")
    data = json.loads(r)
    assert data["success"] is True
    sid = data["session_id"]

    out = json.loads(await tool.execute(operation="read", session_id=sid, wait=0.7))
    assert out["success"] is True
    assert out["running"] is True
    assert "123" in out["output"]

    listing = json.loads(await tool.execute(operation="list"))["sessions"]
    assert any(s["id"] == sid and s["running"] for s in listing)

    stop = json.loads(await tool.execute(operation="stop", session_id=sid))
    assert stop["success"] is True
    stop_all_sessions()


@pytest.mark.asyncio
async def test_terminal_unknown_operation_and_session():
    tool = TerminalTool()
    assert json.loads(await tool.execute(operation="nope"))["success"] is False
    assert json.loads(await tool.execute(operation="read", session_id="x"))["success"] is False
    assert json.loads(await tool.execute(operation="start", command=""))["success"] is False
