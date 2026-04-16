import pytest
import os
import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


@pytest.fixture
def tmp_workspace(tmp_path):
    """创建临时 workspace 目录结构"""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "agents").mkdir()
    (ws / "skills").mkdir()
    (ws / "memory").mkdir()
    (ws / "memory" / "agents").mkdir()
    return str(ws)


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
