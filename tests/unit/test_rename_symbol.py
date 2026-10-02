"""rename_symbol：整词回退重命名（LSP 路径需真实服务器，此处覆盖回退）。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tools.rename_symbol import RenameSymbolTool  # noqa: E402


@pytest.mark.asyncio
async def test_textual_rename_apply(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    return foo()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("foo()\n", encoding="utf-8")
    tool = RenameSymbolTool()
    tool.workspace = str(tmp_path)

    r = await tool.execute(symbol="foo", new_name="bar", apply=True)
    data = json.loads(r)
    assert data["success"] is True and data["via"] == "textual"
    assert data["total_replacements"] == 3
    assert "bar" in (tmp_path / "a.py").read_text()
    assert "foo" not in (tmp_path / "a.py").read_text()
    assert (tmp_path / "b.py").read_text().strip() == "bar()"


@pytest.mark.asyncio
async def test_textual_rename_dry_run(tmp_path):
    (tmp_path / "a.py").write_text("foo()\n", encoding="utf-8")
    tool = RenameSymbolTool()
    tool.workspace = str(tmp_path)
    data = json.loads(await tool.execute(symbol="foo", new_name="bar", apply=False))
    assert data["dry_run"] is True
    assert (tmp_path / "a.py").read_text() == "foo()\n"  # 未写盘


@pytest.mark.asyncio
async def test_rename_validations(tmp_path):
    tool = RenameSymbolTool()
    tool.workspace = str(tmp_path)
    assert json.loads(await tool.execute(symbol="x", new_name="1bad"))["success"] is False
    assert json.loads(await tool.execute(symbol="nope", new_name="y"))["success"] is False
