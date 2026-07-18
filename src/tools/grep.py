import os
import re
import json
import fnmatch
import logging
from typing import Dict, Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


class GrepTool(BuiltinTool):
    """文件内容搜索工具 — 在指定目录中递归搜索匹配正则表达式的内容"""

    MAX_MATCH_OUTPUT_CHARS = 50000

    @staticmethod
    def _truncate_matches(matches, max_chars=MAX_MATCH_OUTPUT_CHARS):
        """截断匹配结果，防止撑爆上下文"""
        total = 0
        kept = []
        for m in matches:
            entry_chars = len(m.get("content", "")) + len(m.get("context", "")) + 100
            if total + entry_chars > max_chars:
                break
            kept.append(m)
            total += entry_chars
        return kept

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return """通用文件内容搜索工具。基于正则表达式搜索文件中的文本，支持所有文件类型。

【和 code_search 的区别】
- grep: 基于正则表达式的全文搜索，**支持所有文件类型**（.py/.ts/.js/.json/.md 等）
- code_search: 基于 AST 的结构化搜索，**仅限 Python**，能理解函数/类定义、调用关系

使用场景：
- 搜索任意文件中的特定文本、关键字、错误信息
- 跨文件查找所有出现某个函数/变量名的地方
- 查找配置文件、JSON、Markdown 中的特定内容

代码分析时先用 grep 搜到目标行号，再用 file_operation(read, offset=行号) 精确读取，避免读取整个大文件。"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "搜索的正则表达式模式"
                },
                "path": {
                    "type": "string",
                    "description": "搜索的根目录路径，默认当前工作目录"
                },
                "file_pattern": {
                    "type": "string",
                    "description": "文件名过滤模式（glob格式），如 '*.py', '*.{json,yaml}'。默认 '*'",
                    "default": "*"
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "是否忽略大小写",
                    "default": False
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回结果数",
                    "default": 50
                },
                "context_lines": {
                    "type": "integer",
                    "description": "显示匹配行前后的上下文行数",
                    "default": 2
                }
            },
            "required": ["pattern"]
        }

    async def execute(self, **kwargs) -> str:
        pattern = kwargs.get("pattern", "")
        search_path = kwargs.get("path", self.workspace or os.getcwd())
        file_pattern = kwargs.get("file_pattern", "*")
        case_insensitive = kwargs.get("case_insensitive", False)
        max_results = kwargs.get("max_results", 50)
        context_lines = kwargs.get("context_lines", 2)

        if not pattern:
            return json.dumps({"success": False, "error": "搜索模式不能为空"}, ensure_ascii=False)

        if not os.path.exists(search_path):
            return json.dumps({"success": False, "error": f"路径不存在: {search_path}"}, ensure_ascii=False)

        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)
        except re.error as e:
            return json.dumps({"success": False, "error": f"正则表达式错误: {e}"}, ensure_ascii=False)

        matches = []
        files_searched = 0
        skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode'}

        if os.path.isfile(search_path):
            file_matches, _ = self._search_file(search_path, regex, file_pattern, context_lines)
            files_searched = 1
            matches = file_matches[:max_results]
            if len(file_matches) > max_results:
                return json.dumps({
                    "success": True,
                    "pattern": pattern,
                    "files_searched": files_searched,
                    "total_matches": len(matches),
                    "truncated": True,
                    "matches": matches
                }, ensure_ascii=False)
        else:
            for root, dirs, files in os.walk(search_path):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in skip_dirs]

                for filename in files:
                    filepath = os.path.join(root, filename)
                    file_matches, searched = self._search_file(filepath, regex, file_pattern, context_lines)
                    files_searched += searched
                    matches.extend(file_matches)

                    if len(matches) >= max_results:
                        matches = matches[:max_results]
                        return json.dumps({
                            "success": True,
                            "pattern": pattern,
                            "files_searched": files_searched,
                            "total_matches": len(matches),
                            "truncated": True,
                            "matches": matches
                        }, ensure_ascii=False)

        # 截断保护
        truncated = len(matches) > 0 and len(json.dumps(matches, ensure_ascii=False)) > self.MAX_MATCH_OUTPUT_CHARS
        if truncated:
            matches = self._truncate_matches(matches)

        return json.dumps({
            "success": True,
            "pattern": pattern,
            "files_searched": files_searched,
            "total_matches": len(matches),
            "truncated": truncated,
            "matches": matches
        }, ensure_ascii=False)

    def _search_file(self, filepath, regex, file_pattern, context_lines):
        """搜索单个文件，返回 (matches, files_searched)"""
        filename = os.path.basename(filepath)
        if not fnmatch.fnmatch(filename, file_pattern):
            return [], 0

        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except (OSError, PermissionError):
            return [], 0

        matches = []
        for i, line in enumerate(lines):
            if regex.search(line):
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                context = "".join(lines[start:end])
                matches.append({
                    "file": filepath,
                    "line": i + 1,
                    "content": line.rstrip(),
                    "context": context.rstrip()
                })

        return matches, 1
