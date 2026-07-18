"""
结构化代码搜索工具 — 增强版 grep/code_preview

支持:
- 查找函数/类定义（基于 AST）
- 查找调用方
- 查找引用
- 依赖关系追踪
- 跨文件分析

用法:
    # 查找定义
    code_search(query="get_user", target="definition")

    # 查找调用方
    code_search(query="get_user", target="callers")

    # 全面分析
    code_search(query="get_user", target="all")
"""
import ast
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent.tools.code_search")


class CodeSearchTool:
    """结构化代码搜索"""

    @property
    def name(self) -> str:
        return "code_search"

    @property
    def description(self) -> str:
        return """结构化代码搜索工具。支持查找函数/类定义、调用方、引用、依赖分析。
比普通 grep 更智能：能理解代码结构，区分定义和调用，追踪跨文件依赖。

用法:
- 查找定义: {"query": "get_user", "target": "definition"}
- 查找调用方: {"query": "get_user", "target": "callers"}
- 查找引用: {"query": "Config", "target": "references"}
- 全面分析: {"query": "get_user", "target": "all"}
- 指定文件: {"query": "get_user", "target": "definition", "file": "src/main.py"}
- 指定类型: {"query": "UserModel", "target": "definition", "symbol_type": "class"}"""

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "要搜索的符号名称（函数名、类名、变量名）"
                },
                "target": {
                    "type": "string",
                    "enum": ["definition", "callers", "references", "all"],
                    "description": "搜索目标: definition=定义, callers=调用方, references=全部引用, all=全部"
                },
                "file": {
                    "type": "string",
                    "description": "限定搜索范围到指定文件（可选）"
                },
                "symbol_type": {
                    "type": "string",
                    "enum": ["function", "class", "variable", "all"],
                    "description": "符号类型过滤（可选）"
                },
                "workspace": {
                    "type": "string",
                    "description": "工作目录（可选，默认使用当前工作目录）"
                },
            },
            "required": ["query", "target"]
        }

    async def execute(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        target = kwargs.get("target", "all")
        file_path = kwargs.get("file", "")
        symbol_type = kwargs.get("symbol_type", "all")
        workspace = kwargs.get("workspace", "")

        if not query:
            return json.dumps({"success": False, "error": "缺少 query 参数"}, ensure_ascii=False)

        if not workspace:
            workspace = os.getcwd()

        result = {
            "query": query,
            "target": target,
        }

        if target in ("definition", "all"):
            defs = await self._find_definitions(query, workspace, file_path, symbol_type)
            if defs:
                result["definitions"] = defs

        if target in ("callers", "all"):
            callers = await self._find_callers(query, workspace, file_path)
            if callers:
                result["callers"] = callers

        if target in ("references", "all"):
            refs = await self._find_references(query, workspace, file_path)
            if refs:
                result["references"] = refs

        result["success"] = True
        return json.dumps(result, ensure_ascii=False, indent=2)

    async def _find_definitions(self, query: str, workspace: str,
                                 file_path: str = "", symbol_type: str = "all") -> list[dict]:
        """查找定义（使用 AST）"""
        definitions = []

        # 优先用 ripgrep 找到可能包含定义的 Python 文件
        files = self._grep_files(rf"(class |def |async def ){re.escape(query)}", workspace, file_path)

        for file in files[:10]:  # 最多查 10 个文件
            full_path = file if os.path.isabs(file) else os.path.join(workspace, file)
            if not os.path.isfile(full_path):
                continue

            # 使用 AST 解析
            try:
                tree = self._parse_ast(full_path)
                if tree:
                    definitions.extend(self._find_in_ast(tree, query, file, symbol_type, full_path))
            except Exception as e:
                logger.debug(f"AST 解析失败 {file}: {e}")
                # 回退到行级 grep
                defs = self._grep_definitions_fallback(query, file, full_path, symbol_type)
                definitions.extend(defs)

        return definitions

    async def _find_callers(self, query: str, workspace: str, file_path: str = "") -> list[dict]:
        """查找调用方"""
        callers = []

        # 搜索所有引用位置
        files = self._grep_files(re.escape(query), workspace, file_path)

        for file in files[:15]:
            full_path = file if os.path.isabs(file) else os.path.join(workspace, file)
            if not os.path.isfile(full_path):
                continue

            lines = self._get_matching_lines(full_path, query)
            for line_no, line_text in lines:
                # 跳过定义行自身
                if re.match(rf"\s*(class |def |async def ){re.escape(query)}", line_text):
                    continue
                # 跳过 import 行
                if re.match(rf"\s*(import |from .+ import )", line_text):
                    continue

                # 尝试确定当前所属的函数/类上下文
                context = self._get_surrounding_context(full_path, line_no)

                callers.append({
                    "file": file,
                    "line": line_no,
                    "code": line_text.strip()[:200],
                    "context": context,
                })

        return callers

    async def _find_references(self, query: str, workspace: str, file_path: str = "") -> list[dict]:
        """查找所有引用（包括定义、调用、导入）"""
        refs = []

        files = self._grep_files(re.escape(query), workspace, file_path)
        for file in files[:20]:
            full_path = file if os.path.isabs(file) else os.path.join(workspace, file)
            if not os.path.isfile(full_path):
                continue

            lines = self._get_matching_lines(full_path, query)
            for line_no, line_text in lines:
                kind = self._classify_reference(line_text, query, file)
                refs.append({
                    "file": file,
                    "line": line_no,
                    "code": line_text.strip()[:200],
                    "kind": kind,
                })

        return refs

    # ── AST 解析 ───────────────────────────────────────

    @staticmethod
    def _parse_ast(file_path: str):
        """解析 Python 文件为 AST"""
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                return ast.parse(f.read(), filename=file_path)
        except SyntaxError:
            return None

    def _find_in_ast(self, tree, query: str, file_path: str,
                      symbol_type: str, full_path: str) -> list[dict]:
        """在 AST 中查找符号定义"""
        results = []

        for node in ast.walk(tree):
            # 类定义
            if isinstance(node, ast.ClassDef) and node.name == query:
                if symbol_type in ("class", "all"):
                    # 获取基类
                    bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                    # 获取方法列表
                    methods = [n.name for n in node.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    results.append({
                        "kind": "class",
                        "name": node.name,
                        "file": file_path,
                        "line": node.lineno,
                        "bases": bases,
                        "methods": methods[:10],
                        "docstring": ast.get_docstring(node) or "",
                    })

            # 函数定义
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == query:
                if symbol_type in ("function", "all"):
                    # 获取参数列表
                    args = [a.arg for a in node.args.args]
                    # 获取返回值标注
                    returns = None
                    if node.returns:
                        returns = ast.dump(node.returns)[:50]

                    results.append({
                        "kind": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                        "name": node.name,
                        "file": file_path,
                        "line": node.lineno,
                        "args": args,
                        "returns": returns,
                        "docstring": ast.get_docstring(node) or "",
                    })

        return results

    # ── grep 辅助 ─────────────────────────────────────

    def _grep_files(self, pattern: str, workspace: str, file_path: str = "") -> list[str]:
        """grep 搜索包含匹配的文件"""
        search_path = file_path if file_path else workspace
        if not os.path.exists(search_path):
            return []

        try:
            cmd = ["grep", "-rl", "--include=*.py", "--include=*.ts", "--include=*.js",
                   "--include=*.rs", "--include=*.java", "--include=*.go",
                   "--include=*.md", ".", "-e", pattern]
            # 排除 .git 等目录
            if os.path.isdir(search_path):
                result = subprocess.run(
                    cmd + ["--exclude-dir=.git", "--exclude-dir=node_modules",
                           "--exclude-dir=.venv", "--exclude-dir=__pycache__"],
                    cwd=search_path, capture_output=True, text=True, timeout=15,
                )
            else:
                # 单个文件直接返回
                return [search_path]

            files = [f for f in result.stdout.strip().split("\n") if f]
            # 过滤 .agentignore
            try:
                from agent_ignore import AgentIgnore
                ai = AgentIgnore(workspace)
                files = [f for f in files if not ai.should_ignore(f)]
            except ImportError:
                pass
            return files
        except Exception as e:
            logger.debug(f"grep 搜索失败: {e}")
            return []

    @staticmethod
    def _get_matching_lines(file_path: str, query: str) -> list[tuple[int, str]]:
        """获取文件中的匹配行"""
        try:
            result = subprocess.run(
                ["grep", "-n", "-E", re.escape(query), file_path],
                capture_output=True, text=True, timeout=10,
            )
            lines = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                if ":" in line:
                    parts = line.split(":", 1)
                    try:
                        line_no = int(parts[0])
                        lines.append((line_no, parts[1]))
                    except ValueError:
                        lines.append((0, line))
            return lines
        except Exception:
            return []

    @staticmethod
    def _get_surrounding_context(file_path: str, line_no: int, window: int = 3) -> str:
        """获取指定行周围的上下文（用于确定所属函数）"""
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            start = max(0, line_no - window - 1)
            # 向前扫描找最近的函数/类定义
            context = ""
            for i in range(line_no - 2, -1, -1):
                line = lines[i].strip()
                if re.match(r"^(class |def |async def )", line):
                    context = f"在 {line}" + (f" (第{i+1}行)" if i != line_no - 1 else "")
                    break
            return context
        except Exception:
            return ""

    @staticmethod
    def _classify_reference(line_text: str, query: str, file_path: str) -> str:
        """分类引用的类型"""
        if re.match(rf"\s*(class ){re.escape(query)}", line_text):
            return "definition:class"
        if re.match(rf"\s*(def |async def ){re.escape(query)}", line_text):
            return "definition:function"
        if re.match(rf"\s*(import {re.escape(query)}|from .+ import .*{re.escape(query)})", line_text):
            return "import"
        if re.search(rf"{re.escape(query)}\s*\(", line_text):
            return "call"
        if query in line_text:
            return "reference"
        return "unknown"

    def _grep_definitions_fallback(self, query: str, file_path: str,
                                     full_path: str, symbol_type: str) -> list[dict]:
        """AST 解析失败时的回退方案：行级 grep 找定义"""
        results = []
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for i, line in enumerate(lines, 1):
                # 匹配 class/def 定义行
                if symbol_type in ("class", "all"):
                    m = re.match(rf"\s*class ({re.escape(query)})\s*[\(:]", line)
                    if m:
                        results.append({
                            "kind": "class",
                            "name": m.group(1),
                            "file": file_path,
                            "line": i,
                            "code": line.strip()[:200],
                        })
                if symbol_type in ("function", "all"):
                    m = re.match(rf"\s*(?:async )?def ({re.escape(query)})\s*\(", line)
                    if m:
                        results.append({
                            "kind": "function",
                            "name": m.group(1),
                            "file": file_path,
                            "line": i,
                            "code": line.strip()[:200],
                        })
        except Exception:
            pass
        return results
