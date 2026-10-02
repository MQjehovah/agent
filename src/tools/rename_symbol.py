"""符号级重命名工具 — 优先 LSP 的 textDocument/rename，回退项目内整词替换。

- LSP 路径：提供 `line`/`character`(1-based) 且语言服务器可用时，走 LSP 精确重命名
  （含跨文件引用），返回/应用 WorkspaceEdit。
- 回退路径：在 `path` 范围内对受支持代码文件做**整词**替换（\\b 边界），
  改前快照、可 apply=false 预演；返回每个文件的 diff 与替换数。
"""
import json
import logging
import os
import re

from . import BuiltinTool
from .edit import _make_diff, _read_text

logger = logging.getLogger("agent.tools")

_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".agent",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build",
    ".next", "target", "out", ".idea", ".vscode", "coverage",
}


def _supported_exts() -> set[str]:
    try:
        from .repo_map import _SUPPORTED_EXT
        return set(_SUPPORTED_EXT)
    except Exception:  # noqa: BLE001
        return {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".c", ".cpp", ".h", ".hpp"}


def _uri_to_path(uri: str) -> str:
    if uri.startswith("file:///"):
        p = uri[len("file:///"):]
        return p if os.name != "nt" else p.replace("/", "\\")
    return uri


class RenameSymbolTool(BuiltinTool):
    """符号级重命名（LSP 优先 / 整词回退）。"""

    run_on_main_loop = True

    @property
    def name(self) -> str:
        return "rename_symbol"

    @property
    def description(self) -> str:
        return (
            "把符号(symbol)重命名为 new_name。优先用语言服务器(需给 line/character)做精确重命名"
            "（含跨文件引用）；无 LSP 时在 `path` 范围内对受支持代码文件做整词替换。"
            "apply=false 仅预演（返回待改文件与 diff）；默认 apply=true 落盘（改前自动快照）。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "当前符号名（回退路径用）"},
                "new_name": {"type": "string", "description": "新名称"},
                "path": {"type": "string", "description": "范围（文件或目录；默认整个工作区）"},
                "file": {"type": "string", "description": "LSP 重命名目标文件（可选）"},
                "line": {"type": "integer", "description": "LSP: 符号所在行(1-based)"},
                "character": {"type": "integer", "description": "LSP: 列(1-based, 默认 1)"},
                "apply": {"type": "boolean", "description": "是否落盘（默认 true；false=预演）", "default": True},
            },
            "required": ["new_name"],
        }

    async def execute(self, symbol: str = "", new_name: str = "", path: str = "",
                      file: str = "", line: int = 0, character: int = 1,
                      apply: bool = True, **kwargs) -> str:
        new_name = (new_name or "").strip()
        if not new_name or not re.match(r"^[A-Za-z_$][\w$]*$", new_name):
            return self._error("new_name 必须是合法标识符")

        # 1) LSP 精确重命名
        lsp_result = await self._try_lsp(symbol, new_name, file, line, character, apply)
        if lsp_result is not None:
            return lsp_result

        # 2) 整词回退
        symbol = (symbol or "").strip()
        if not symbol or not re.match(r"^[A-Za-z_$][\w$]*$", symbol):
            return self._error("需提供合法 symbol（LSP 不可用时用整词替换）")
        return await self._textual_rename(symbol, new_name, path, apply)

    # ── LSP ─────────────────────────────────────────

    async def _try_lsp(self, symbol, new_name, file, line, character, apply):
        if not file or line <= 0:
            return None
        full = self.resolve_path(file)
        if not os.path.isfile(full):
            return None
        try:
            from lsp import get_lsp_manager, language_for

            if not language_for(full):
                return None
            client = await get_lsp_manager().get_client(full)
            if client is None:
                return None
            edit = await client.rename(full, line, character, new_name)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"LSP rename 不可用，回退整词: {e}")
            return None
        changes = (edit or {}).get("changes") or {}
        if not apply:
            return json.dumps({"success": True, "via": "lsp", "dry_run": True,
                               "workspace_edit": edit}, ensure_ascii=False)
        applied = await self._apply_workspace_edit(changes)
        return json.dumps({"success": True, "via": "lsp",
                           "applied_files": applied}, ensure_ascii=False)

    async def _apply_workspace_edit(self, changes: dict) -> list[str]:
        applied = []
        for uri, edits in (changes or {}).items():
            path = _uri_to_path(uri)
            if not os.path.isfile(path):
                continue
            content, encoding = _read_text(path)
            await self._backup(path, content)
            # 逆序应用，偏移不互相影响
            new = content
            for e in sorted(edits, key=lambda x: (
                    x["range"]["start"]["line"], x["range"]["start"]["character"]), reverse=True):
                start = _offset(new, e["range"]["start"])
                end = _offset(new, e["range"]["end"])
                new = new[:start] + e.get("newText", "") + new[end:]
            with open(path, "w", encoding=encoding, newline="") as f:
                f.write(new)
            applied.append(path)
        return applied

    # ── 整词回退 ────────────────────────────────────

    async def _textual_rename(self, symbol: str, new_name: str, path: str, apply: bool) -> str:
        root = self.resolve_path(path) if path else (self.workspace or os.getcwd())
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        exts = _supported_exts()
        files: list[dict] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.splitext(name)[1].lower() not in exts:
                    continue
                try:
                    content, encoding = _read_text(full)
                except Exception:  # noqa: BLE001
                    continue
                n = len(pattern.findall(content))
                if n == 0:
                    continue
                new_content = pattern.sub(new_name, content)
                rel = os.path.relpath(full, self.workspace or os.getcwd()).replace("\\", "/")
                files.append({"file": rel, "replacements": n,
                              "diff": _make_diff(full, content, new_content)})
                total += n
                if apply:
                    await self._backup(full, content)
                    with open(full, "w", encoding=encoding, newline="") as f:
                        f.write(new_content)
        if total == 0:
            return self._error(f"未找到符号: {symbol}")
        return json.dumps({
            "success": True, "via": "textual", "dry_run": not apply,
            "symbol": symbol, "new_name": new_name, "total_replacements": total,
            "files": files,
        }, ensure_ascii=False)

    async def _backup(self, path: str, content: str):
        try:
            from agent.core import current_run
            from undo_manager import UndoManager
            rc = current_run()
            ws = self.workspace or (rc.task_dir if rc else "")
            if ws and os.path.exists(ws):
                await UndoManager(ws).snapshot_before_edit(path, content)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"rename 快照失败(忽略): {e}")

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)


def _offset(text: str, pos: dict) -> int:
    """把 LSP {line,character}(0-based) 转为字符偏移。"""
    line = pos.get("line", 0)
    char = pos.get("character", 0)
    if line <= 0:
        return char
    idx = 0
    seen = 0
    while seen < line and idx < len(text):
        if text[idx] == "\n":
            seen += 1
        idx += 1
    return min(idx + char, len(text))
