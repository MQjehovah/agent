"""后台/流式终端工具：start/read/list/stop。"""
import asyncio
import json
import os
import sys
import time

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


@pytest.mark.asyncio
async def test_terminal_buffer_is_capped(tmp_path):
    """后台进程输出不能无限攒在内存里（历史事故：36GB 缓冲区）。"""
    script = tmp_path / "spam.py"
    script.write_text(
        "import sys\nsys.stdout.write('x' * 5000000)\nsys.stdout.flush()\n",
        encoding="utf-8",
    )
    tool = TerminalTool()
    try:
        started = await tool.execute(
            operation="start", command=f"{sys.executable} {script}", cwd=str(tmp_path)
        )
    except NotImplementedError:
        pytest.skip("当前事件循环不支持子进程(Windows Selector loop)")
    sid = json.loads(started)["session_id"]

    entry = None
    deadline = time.time() + 30
    while time.time() < deadline:
        listing = json.loads(await tool.execute(operation="list"))["sessions"]
        entry = next((s for s in listing if s["id"] == sid), None)
        if entry and not entry["running"]:
            break
        await asyncio.sleep(0.2)
    stop_all_sessions()

    assert entry is not None
    assert entry["buffered_chars"] <= 1_000_000, (
        f"缓冲区保留了 {entry['buffered_chars']} 字符，没有上限"
    )
