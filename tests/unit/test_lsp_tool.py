"""LSP 工具：语言识别、JSON-RPC 帧、服务器不可用时降级。"""
import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import lsp.client as lc  # noqa: E402
from lsp.client import encode_message, language_for  # noqa: E402
from tools.lsp import LspTool  # noqa: E402


def test_language_for():
    assert language_for("a.py") == "python"
    assert language_for("a.ts") == "typescript"
    assert language_for("a.go") == "go"
    assert language_for("a.txt") == ""


def test_encode_message_framing():
    raw = encode_message({"jsonrpc": "2.0", "id": 1, "method": "x"})
    assert raw.startswith(b"Content-Length: ")
    head, body = raw.split(b"\r\n\r\n", 1)
    assert int(head.split(b":", 1)[1]) == len(body)


@pytest.mark.asyncio
async def test_lsp_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "server_command", lambda lang: None)
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
    tool = LspTool()
    tool.workspace = str(tmp_path)

    hover = json.loads(await tool.execute(operation="hover", file="a.py", line=1))
    assert hover["success"] is False
    assert "语言服务器" in hover["error"]

    if importlib.util.find_spec("ruff") is None:
        pytest.skip("ruff not installed")
    diag = json.loads(await tool.execute(operation="diagnostics", file="a.py"))
    assert diag["success"] is True
    assert diag.get("fallback") == "code_diagnostics"


@pytest.mark.asyncio
async def test_lsp_unsupported_type(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    tool = LspTool()
    tool.workspace = str(tmp_path)
    data = json.loads(await tool.execute(operation="diagnostics", file="a.txt"))
    assert data["success"] is False
