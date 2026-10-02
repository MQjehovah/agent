"""edit 跨文件原子编辑：合并 batch_edit 的能力 + hash 锚点。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tools.edit import EditTool  # noqa: E402


def _tool(tmp_path):
    t = EditTool()
    t.workspace = str(tmp_path)
    return t


@pytest.mark.asyncio
async def test_cross_file_edit(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bar\n", encoding="utf-8")
    t = _tool(tmp_path)

    r = await t.execute(edits=[
        {"file": "a.txt", "old_string": "foo", "new_string": "FOO"},
        {"file": "b.txt", "old_string": "bar", "new_string": "BAR"},
    ])
    data = json.loads(r)
    assert data["success"] is True
    assert set(data["applied_files"]) == {"a.txt", "b.txt"}
    assert (tmp_path / "a.txt").read_text() == "FOO\n"
    assert (tmp_path / "b.txt").read_text() == "BAR\n"


@pytest.mark.asyncio
async def test_cross_file_atomic_rejects_all_on_bad_anchor(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("bar\n", encoding="utf-8")
    t = _tool(tmp_path)

    r = await t.execute(edits=[
        {"file": "a.txt", "old_string": "foo", "new_string": "FOO"},
        {"file": "b.txt", "old_string": "NOPE", "new_string": "X"},
    ])
    data = json.loads(r)
    assert data["success"] is False
    assert (tmp_path / "a.txt").read_text() == "foo\n"  # 未写盘
    assert (tmp_path / "b.txt").read_text() == "bar\n"


@pytest.mark.asyncio
async def test_cross_file_hash_mismatch_rejected(tmp_path):
    (tmp_path / "a.txt").write_text("foo\n", encoding="utf-8")
    t = _tool(tmp_path)
    r = await t.execute(edits=[
        {"file": "a.txt", "old_string": "foo", "new_string": "FOO", "hash": "deadbeefdeadbeef"},
    ])
    assert json.loads(r)["success"] is False


@pytest.mark.asyncio
async def test_cross_file_path_escape_rejected(tmp_path):
    t = _tool(tmp_path)
    r = await t.execute(edits=[{"file": "../outside.txt", "old_string": "x", "new_string": "y"}])
    assert json.loads(r)["success"] is False


def test_same_file_edits_without_path_errors(tmp_path):
    t = _tool(tmp_path)
    import asyncio
    out = asyncio.run(t.execute(edits=[{"old_string": "a", "new_string": "b"}]))
    assert json.loads(out)["success"] is False


def test_batch_edit_not_registered():
    """batch_edit 已删除，不再是工具。"""
    from tools import ToolRegistry
    r = ToolRegistry()
    r.auto_discover()
    assert r.has_tool("batch_edit") is False
    assert "batch_edit" not in r.list_tools()
