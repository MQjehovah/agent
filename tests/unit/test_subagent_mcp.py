"""方案B 测试：子代理 MCP 自加载。

验证子代理按 mcp_servers 参数（或自己 config_dir 的 mcp_servers.json）加载 MCP，
主代理仍读 config_dir/mcp_servers.json，运行时参数优先于配置文件。
MCPManager 用 mock，不启动真实 stdio 子进程。
"""
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent import Agent  # noqa: E402


def _mock_mcp_manager(monkeypatch):
    """mock mcps.MCPManager，返回可断言的 MagicMock。"""
    mgr = MagicMock()
    mgr.connect_server = AsyncMock(return_value=True)
    mgr.start_health_check = MagicMock()
    mgr.close = AsyncMock()
    monkeypatch.setattr("mcps.MCPManager", lambda *a, **k: mgr)
    return mgr


@pytest.mark.asyncio
async def test_subagent_loads_passed_mcp_servers(tmp_path, monkeypatch):
    """子代理加载运行时传入的 mcp_servers（方案B 核心能力）。"""
    mgr = _mock_mcp_manager(monkeypatch)
    agent = Agent(
        workspace=str(tmp_path), client=MagicMock(),
        parent_agent=MagicMock(), config_dir=str(tmp_path),
        mcp_servers=[{"name": "git-mcp", "command": "x", "enabled": True}],
    )
    await agent._load_mcp_servers()
    assert agent.mcp is mgr
    mgr.connect_server.assert_awaited()


@pytest.mark.asyncio
async def test_subagent_without_mcp_does_not_load(tmp_path, monkeypatch):
    """子代理无 mcp_servers 且 config_dir 无 mcp_servers.json → 不加载（保持原行为）。"""
    _mock_mcp_manager(monkeypatch)
    agent = Agent(
        workspace=str(tmp_path), client=MagicMock(),
        parent_agent=MagicMock(), config_dir=str(tmp_path),
    )
    await agent._load_mcp_servers()
    assert agent.mcp is None


@pytest.mark.asyncio
async def test_main_agent_reads_config_dir_mcp(tmp_path, monkeypatch):
    """主代理（无 parent）读 config_dir/mcp_servers.json（回归：未破坏主代理）。"""
    mgr = _mock_mcp_manager(monkeypatch)
    with open(os.path.join(str(tmp_path), "mcp_servers.json"), "w", encoding="utf-8") as f:
        json.dump([{"name": "main-mcp", "command": "x", "enabled": True}], f)
    agent = Agent(workspace=str(tmp_path), client=MagicMock(), config_dir=str(tmp_path))
    await agent._load_mcp_servers()
    assert agent.mcp is mgr
    mgr.connect_server.assert_awaited()


@pytest.mark.asyncio
async def test_subagent_reads_own_config_dir_mcp(tmp_path, monkeypatch):
    """子代理无运行时 mcp_servers 时，读自己 config_dir 的 mcp_servers.json（模板专属 MCP）。"""
    mgr = _mock_mcp_manager(monkeypatch)
    with open(os.path.join(str(tmp_path), "mcp_servers.json"), "w", encoding="utf-8") as f:
        json.dump([{"name": "sub-mcp", "command": "x", "enabled": True}], f)
    agent = Agent(
        workspace=str(tmp_path), client=MagicMock(),
        parent_agent=MagicMock(), config_dir=str(tmp_path),
    )
    await agent._load_mcp_servers()
    assert agent.mcp is mgr


@pytest.mark.asyncio
async def test_runtime_mcp_overrides_config_file(tmp_path, monkeypatch):
    """运行时传入的 mcp_servers 优先于 config_dir 文件。"""
    mgr = _mock_mcp_manager(monkeypatch)
    with open(os.path.join(str(tmp_path), "mcp_servers.json"), "w", encoding="utf-8") as f:
        json.dump([{"name": "file-mcp", "command": "x", "enabled": True}], f)
    agent = Agent(
        workspace=str(tmp_path), client=MagicMock(),
        parent_agent=MagicMock(), config_dir=str(tmp_path),
        mcp_servers=[{"name": "runtime-mcp", "command": "y", "enabled": True}],
    )
    await agent._load_mcp_servers()
    names = [c.args[0].get("name") for c in mgr.connect_server.await_args_list]
    assert "runtime-mcp" in names
    assert "file-mcp" not in names
