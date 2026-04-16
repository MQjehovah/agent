import os
import json
import fnmatch
import logging
from typing import Dict, Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


class GlobTool(BuiltinTool):
    """文件名模式搜索工具 — 按 glob 模式查找文件"""

    @property
    def name(self) -> str:
        return "glob"

    @property
    def description(self) -> str:
        return """按文件名模式快速查找文件。支持 glob 通配符：* 匹配任意字符，? 匹配单个字符，** 匹配多级目录。

使用场景：
- 查找项目中所有 Python 文件：'**/*.py'
- 查找特定名称的配置文件：'**/config*.json'
- 查找测试文件：'**/test_*.py'
- 查找某个目录下的所有文件：'src/**/*'"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "glob 匹配模式，如 '**/*.py', 'src/**/*.json', '*.md'"
                },
                "path": {
                    "type": "string",
                    "description": "搜索的根目录，默认当前工作目录"
                },
                "max_results": {
                    "type": "integer",
                    "description": "最大返回文件数",
                    "default": 100
                }
            },
            "required": ["pattern"]
        }

    async def execute(self, **kwargs) -> str:
        pattern = kwargs.get("pattern", "")
        search_path = kwargs.get("path", os.getcwd())
        max_results = kwargs.get("max_results", 100)

        if not pattern:
            return json.dumps({"success": False, "error": "模式不能为空"}, ensure_ascii=False)

        if not os.path.isdir(search_path):
            return json.dumps({"success": False, "error": f"目录不存在: {search_path}"}, ensure_ascii=False)

        matches = []
        skip_dirs = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode'}

        # 处理 ** 模式（递归搜索）
        if "**" in pattern:
            # 提取 ** 后的文件名部分
            parts = pattern.split("**")
            suffix = parts[-1].lstrip("/\\").lstrip(os.sep) if len(parts) > 1 else pattern

            for root, dirs, files in os.walk(search_path):
                dirs[:] = [d for d in dirs if not d.startswith('.') and d not in skip_dirs]
                for filename in files:
                    if fnmatch.fnmatch(filename, suffix) or fnmatch.fnmatch(filename, pattern):
                        matches.append(os.path.join(root, filename))
                        if len(matches) >= max_results:
                            break
                if len(matches) >= max_results:
                    break
        else:
            # 简单模式，匹配当前目录
            try:
                for filename in os.listdir(search_path):
                    if fnmatch.fnmatch(filename, pattern):
                        matches.append(os.path.join(search_path, filename))
                        if len(matches) >= max_results:
                            break
            except PermissionError:
                pass

        return json.dumps({
            "success": True,
            "pattern": pattern,
            "path": search_path,
            "count": len(matches),
            "files": sorted(matches)
        }, ensure_ascii=False)
