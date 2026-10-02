"""LSP 工具 — 编辑器级能力：实时诊断 / hover / 跳转定义 / 查找引用 / 重命名。

依赖语言服务器（pyright-langserver / typescript-language-server / gopls / rust-analyzer）
或经 `AGENT_LSP_SERVERS`（JSON: 语言→命令数组）配置。服务器不可用时：
- diagnostics 回退 `code_diagnostics`（ruff/mypy/eslint/tsc…）；
- 其它操作返回明确提示。
"""
import asyncio
import json
import logging
import os

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class LspTool(BuiltinTool):
    """查询语言服务器：诊断/悬停/定义/引用/重命名。"""

    # 语言服务器是长驻进程，须挂主事件循环（跨调用复用）
    run_on_main_loop = True

    @property
    def name(self) -> str:
        return "lsp"

    @property
    def description(self) -> str:
        return (
            "语言服务器(LSP)查询：operation=diagnostics 取实时诊断(错误/警告)；"
            "hover 看类型/文档；definition 跳转定义；references 查引用；"
            "rename 生成重命名编辑(new_name)。"
            "需要目标语言服务器已安装（pyright/ts-ls/gopls/rust-analyzer）。"
            "line/character 为 1-based。diagnostics 在无 LSP 时回退 code_diagnostics。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["diagnostics", "hover", "definition", "references", "rename"],
                    "description": "操作类型",
                },
                "file": {"type": "string", "description": "目标文件（相对工作区或绝对路径）"},
                "line": {"type": "integer", "description": "行号(1-based, hover/definition/references/rename 需要)"},
                "character": {"type": "integer", "description": "列号(1-based, 默认 1)"},
                "new_name": {"type": "string", "description": "operation=rename 时的新名称"},
            },
            "required": ["operation", "file"],
        }

    async def execute(self, operation: str = "", file: str = "", line: int = 0,
                      character: int = 1, new_name: str = "", **kwargs) -> str:
        op = (operation or "").strip().lower()
        if op not in ("diagnostics", "hover", "definition", "references", "rename"):
            return self._error(f"未知 operation: {operation!r}")

        full = self.resolve_path(file)
        if not os.path.isfile(full):
            return self._error(f"文件不存在: {file}")

        from lsp import get_lsp_manager, language_for

        lang = language_for(full)
        if not lang:
            return self._error(f"不支持的文件类型: {os.path.splitext(full)[1]}")

        client = await get_lsp_manager().get_client(full)
        if client is None:
            if op == "diagnostics":
                return await self._diagnostics_fallback(full, file)
            return self._error(
                "未找到可用的语言服务器。请安装对应语言的 LSP 服务器，"
                "或用 AGENT_LSP_SERVERS 指定（JSON: 语言→命令数组）")

        try:
            if op == "diagnostics":
                return await self._diagnostics(client, full, file)
            if line <= 0:
                return self._error(f"operation={op} 需要 line(1-based)")
            if op == "hover":
                result = await client.hover(full, line, character)
                return json.dumps({"success": True, "operation": op, "result": result},
                                  ensure_ascii=False)
            if op == "definition":
                result = await client.definition(full, line, character)
                return json.dumps({"success": True, "operation": op, "result": result},
                                  ensure_ascii=False)
            if op == "references":
                result = await client.references(full, line, character)
                return json.dumps({"success": True, "operation": op, "result": result},
                                  ensure_ascii=False)
            # rename
            if not new_name:
                return self._error("operation=rename 需要 new_name")
            result = await client.rename(full, line, character, new_name)
            return json.dumps({"success": True, "operation": "rename",
                               "workspace_edit": result}, ensure_ascii=False)
        except asyncio.TimeoutError:
            return self._error("LSP 请求超时")
        except Exception as e:  # noqa: BLE001
            return self._error(f"LSP 请求失败: {e}")

    async def _diagnostics(self, client, full: str, rel: str) -> str:
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception as e:  # noqa: BLE001
            return self._error(f"读取文件失败: {e}")
        uri = await client.did_open(full, text)
        diags = await client.wait_diagnostics(uri)
        issues = [{
            "line": (d.get("range", {}).get("start", {}) or {}).get("line", 0) + 1,
            "character": (d.get("range", {}).get("start", {}) or {}).get("character", 0) + 1,
            "severity": _SEVERITY.get(d.get("severity", 1), "error"),
            "message": d.get("message", ""),
            "source": d.get("source", "lsp"),
        } for d in diags]
        return json.dumps({"success": True, "operation": "diagnostics", "file": rel,
                           "issue_count": len(issues), "issues": issues}, ensure_ascii=False)

    async def _diagnostics_fallback(self, full: str, rel: str) -> str:
        """无 LSP 时回退 code_diagnostics。"""
        from tools.diagnostics import run_diagnostics
        try:
            result = await asyncio.to_thread(
                run_diagnostics, self.workspace or os.getcwd(), [rel], None, 60)
        except Exception as e:  # noqa: BLE001
            return self._error(f"诊断失败: {e}")
        return json.dumps({
            "success": True, "operation": "diagnostics", "fallback": "code_diagnostics",
            "note": "未找到语言服务器，已回退 code_diagnostics",
            "results": result.get("results", []),
        }, ensure_ascii=False)

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
