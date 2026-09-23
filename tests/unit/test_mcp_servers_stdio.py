"""W3/W3b 五个新 server 的 v2 stdio 子进程冒烟。

每个 server 以 `sys.executable mcp_server/src/mcp_X.py` 真起子进程(cwd=仓库根),
注入假凭证规避 env_guard, 在 10s 超时内完成 initialize + list_tools 并断言工具数;
只列工具不调用工具, 不触网/不连 DB。跳过条件仅限本机没有可用 python 解释器。
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "mcp_server" / "src"
PYTHON = sys.executable

SERVERS = [
    ("mcp_time.py", 3),
    ("mcp_fetch.py", 2),
    ("mcp_filesystem.py", 8),
    ("mcp_git.py", 11),
    ("mcp_postgres.py", 3),
]

FAKE_ENV = {
    "PYTHONUNBUFFERED": "1",
    "APP_ENV": "development",
    "MCP_FETCH_ALLOW_HOSTS": "",
    "FS_MCP_ROOTS": "",
    "FS_MCP_ALLOW_WRITE": "false",
    "GIT_MCP_ROOTS": "",
    "GIT_MCP_ALLOW_WRITE": "false",
    "PG_MCP_DSN": "postgresql://mcp_stdio_test:fake-pass@127.0.0.1:1/rd_stdio_test",
}


@pytest.mark.skipif(not PYTHON or not Path(PYTHON).exists(), reason="本机无可用 python 解释器")
@pytest.mark.parametrize("script,expected_tools", SERVERS)
async def test_new_server_stdio_initialize_and_list_tools(script, expected_tools):
    params = StdioServerParameters(
        command=PYTHON,
        args=[str(MCP_SRC / script)],
        env={**os.environ, **FAKE_ENV},
        cwd=str(ROOT),
    )

    async def smoke():
        async with (
            stdio_client(params, errlog=subprocess.DEVNULL) as (read, write),
            ClientSession(read, write) as session,
        ):
            init = await session.initialize()
            tools = await session.list_tools()
            return init, tools

    init, tools = await asyncio.wait_for(smoke(), timeout=10)
    assert init.server_info is not None
    assert len(tools.tools) == expected_tools
    assert all(tool.input_schema is not None for tool in tools.tools)
