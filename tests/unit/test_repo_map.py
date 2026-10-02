"""repo_map：符号地图抽取、目录跳过、.agentignore 过滤。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tools.repo_map import _CACHE, RepoMapTool  # noqa: E402


def _sym_names(data):
    return [s["name"] for f in data["files"] for s in f["symbols"]]


@pytest.mark.asyncio
async def test_repo_map_symbols_and_skip_dirs(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n\nclass Bar:\n    pass\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("export function baz() {}\nclass Qux {}\n", encoding="utf-8")
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "x.py").write_text("def hidden():\n    pass\n", encoding="utf-8")

    _CACHE.clear()
    tool = RepoMapTool()
    tool.workspace = str(tmp_path)
    r = await tool.execute()
    data = json.loads(r)
    assert data["success"] is True
    paths = {f["path"] for f in data["files"]}
    assert "a.py" in paths and "b.js" in paths
    assert not any("node_modules" in p for p in paths)
    names = _sym_names(data)
    assert "foo" in names and "Bar" in names
    assert "hidden" not in names


@pytest.mark.asyncio
async def test_repo_map_respects_agentignore(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    pass\n", encoding="utf-8")
    (tmp_path / ".agentignore").write_text("b.py\n", encoding="utf-8")

    _CACHE.clear()
    tool = RepoMapTool()
    tool.workspace = str(tmp_path)
    data = json.loads(await tool.execute())
    paths = {f["path"] for f in data["files"]}
    assert "a.py" in paths
    assert "b.py" not in paths


@pytest.mark.asyncio
async def test_repo_map_missing_dir(tmp_path):
    tool = RepoMapTool()
    tool.workspace = str(tmp_path)
    data = json.loads(await tool.execute(path="nope"))
    assert data["success"] is False
