"""shell 工具：超时必须真正杀死子进程，输出必须限内存。"""
import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tools import shell as shell_module  # noqa: E402
from tools.shell import ShellTool  # noqa: E402


def test_read_pipe_bounded_keeps_at_most_limit_and_drains():
    """读取管道时最多保留 limit 字节，但必须读到 EOF，避免子进程写阻塞。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 3000000)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        data = shell_module.read_pipe_bounded(proc.stdout, 100000)
    finally:
        proc.stdout.close()
        proc.wait(timeout=30)
    assert len(data) <= 100000, f"内存中保留了 {len(data)} 字节，超过上限"
    assert proc.returncode == 0, "子进程应能正常写完退出（读取方必须排空管道）"


@pytest.mark.asyncio
async def test_shell_timeout_kills_child_process(tmp_path):
    """命令超时后子进程必须被杀掉，不能继续在后台跑并持续产出。"""
    marker = tmp_path / "ticks.txt"
    script = tmp_path / "tick.py"
    script.write_text(
        "import time, pathlib\n"
        f"p = pathlib.Path(r'{marker}')\n"
        "end = time.time() + 6\n"
        "while time.time() < end:\n"
        "    with p.open('a') as f:\n"
        "        f.write('x')\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )

    tool = ShellTool()
    result = json.loads(
        await tool.execute(command=f"{sys.executable} {script}", timeout=1, cwd=str(tmp_path))
    )
    assert result["success"] is False
    assert "超时" in result["error"]

    time.sleep(0.5)
    size_before = marker.stat().st_size
    time.sleep(1.0)
    size_after = marker.stat().st_size
    assert size_after == size_before, (
        f"子进程仍在运行并写入文件: {size_before} -> {size_after}"
    )
