"""ToolRegistry：JSON 错误信封、重命名后的注册集合。"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tools import ToolRegistry  # noqa: E402


def test_unknown_tool_returns_json_error():
    r = ToolRegistry()
    out = asyncio.run(r.execute("nope", {}))
    data = json.loads(out)
    assert data["success"] is False and "未找到" in data["error"]


def test_registered_tool_names_after_renames():
    r = ToolRegistry()
    r.auto_discover()
    names = r.list_tools()
    # 已重命名/合并后的工具名
    assert "edit" in names
    assert "tool_search" in names
    assert "read_image" in names
    assert "task" in names
    # 已删除/合并的旧名不再注册
    assert "batch_edit" not in names
    assert "search_tools" not in names
    assert "view_image" not in names
    assert "subagent" not in names
