import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# 固定把应用源码目录加入 sys.path：部分测试文件只加了 mcp_server/src，
# 依赖其它文件先 import 才偶然可用（曾致 test_mcp_servers_v2 单跑全错）。
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture
def tmp_workspace(tmp_path):
    """创建临时 workspace 目录结构"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "agents").mkdir()
    (ws / "skills").mkdir()
    return str(ws)


@pytest.fixture(autouse=True)
def _init_settings(tmp_path):
    """初始化 settings 单例（Agent.__init__ 等处读取配置，避免依赖测试执行顺序）。"""
    from settings import init_settings
    init_settings(str(tmp_path))


@pytest.fixture
def mock_llm_client():
    """Mock LLM 客户端"""
    client = MagicMock()

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = "测试回复"
    mock_response.choices[0].message.tool_calls = None
    mock_response.usage = MagicMock()
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    client.chat = AsyncMock(return_value=mock_response)
    client.stream_chat = AsyncMock(return_value=iter([]))
    client.usage_tracker = MagicMock()
    client.usage_tracker.track = MagicMock()
    client.usage_tracker.get_summary = MagicMock(return_value={
        "total_calls": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_tokens": 0,
        "total_cost_cny": 0.0,
    })
    return client


@pytest.fixture
def sample_prompt_md(tmp_workspace):
    """创建示例 PROMPT.md"""
    prompt_file = os.path.join(tmp_workspace, "PROMPT.md")
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write("---\nname: 测试代理\ndescription: 测试用\n---\n\n你是测试代理。")
    return prompt_file
