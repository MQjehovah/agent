import os
import re
import json
import logging
from typing import Dict, Any, List

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

MAX_PREVIEW_LINES = 50
MAX_SUMMARY_TOKENS = 2000


class CodePreviewTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "code_preview"

    @property
    def description(self) -> str:
        return (
            "代码预览工具。智能分析代码文件结构，返回函数、类、导入等元信息，"
            "帮助快速定位关键代码。适合在读取大文件前先了解其结构。"
        )

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "代码文件的绝对路径"
                },
                "mode": {
                    "type": "string",
                    "enum": ["structure", "preview", "search"],
                    "default": "structure",
                    "description": "分析模式: structure-返回函数/类结构, preview-返回文件头部预览, search-搜索函数定义"
                },
                "pattern": {
                    "type": "string",
                    "description": "搜索模式(仅search模式): 函数名或类名片段"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, mode: str = "structure", pattern: str = None) -> str:
        try:
            if not os.path.exists(path):
                return json.dumps({"success": False, "error": f"文件不存在: {path}"}, ensure_ascii=False)

            if os.path.isdir(path):
                return json.dumps({"success": False, "error": f"路径是目录: {path}"}, ensure_ascii=False)

            ext = os.path.splitext(path)[1].lower()
            if ext not in [".py", ".js", ".ts", ".tsx", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".rb", ".php", ".swift", ".kt", ".scala", ".lua", ".sh", ".vue", ".jsx", ".css", ".scss", ".html", ".md", ".json", ".yaml", ".yml", ".toml"]:
                return json.dumps({"success": False, "error": f"不支持的文件类型: {ext}"}, ensure_ascii=False)

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            if mode == "structure":
                return self._analyze_structure(path, lines, ext)
            elif mode == "preview":
                return self._preview_file(path, lines)
            elif mode == "search":
                return self._search_pattern(path, lines, pattern, ext)
            else:
                return json.dumps({"success": False, "error": f"未知模式: {mode}"}, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    def _preview_file(self, path: str, lines: List[str]) -> str:
        preview_lines = lines[:MAX_PREVIEW_LINES]
        preview_content = "\n".join(
            f"{i + 1:6d}\t{line.rstrip()}"
            for i, line in enumerate(preview_lines)
        )

        return json.dumps({
            "success": True,
            "path": path,
            "total_lines": len(lines),
            "preview_lines": len(preview_lines),
            "preview": preview_content,
            "hint": "如果需要查看更多内容，请使用 file_operation 工具配合 offset/limit 参数分段读取"
        }, ensure_ascii=False)

    def _analyze_structure(self, path: str, lines: List[str], ext: str) -> str:
        structure = {
            "imports": [],
            "classes": [],
            "functions": [],
            "constants": [],
            "exports": []
        }

        for i, line in enumerate(lines):
            stripped = line.strip()

            if ext == ".py":
                if stripped.startswith("import ") or stripped.startswith("from "):
                    structure["imports"].append({"line": i + 1, "content": stripped[:80]})
                elif re.match(r"^class\s+\w+", stripped):
                    match = re.match(r"^class\s+(\w+)", stripped)
                    structure["classes"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })
                elif re.match(r"^def\s+\w+", stripped) and not stripped.startswith("def _"):
                    match = re.match(r"^def\s+(\w+)", stripped)
                    structure["functions"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })
                elif re.match(r"^@|^export|^public", stripped):
                    structure["exports"].append({"line": i + 1, "content": stripped[:80]})

            elif ext in [".js", ".ts", ".tsx", ".jsx", ".vue"]:
                if re.match(r"^import\s+", stripped) or re.match(r"^require\(", stripped):
                    structure["imports"].append({"line": i + 1, "content": stripped[:80]})
                elif re.match(r"^class\s+\w+", stripped):
                    match = re.match(r"^class\s+(\w+)", stripped)
                    structure["classes"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })
                elif re.match(r"^function\s+\w+|^const\s+\w+\s*=\s*(?:async\s*)?\(?|^export\s+(?:function|const|class)", stripped):
                    match = re.search(r"(?:function|const|class)\s+(\w+)", stripped)
                    structure["functions"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })
                elif re.match(r"^export\s+", stripped):
                    structure["exports"].append({"line": i + 1, "content": stripped[:80]})

            elif ext in [".java", ".kt", ".scala"]:
                if re.match(r"^import\s+", stripped):
                    structure["imports"].append({"line": i + 1, "content": stripped[:80]})
                elif re.match(r"^(?:public|private|protected)?\s*class\s+\w+", stripped):
                    match = re.search(r"class\s+(\w+)", stripped)
                    structure["classes"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })
                elif re.match(r"^(?:public|private|protected)?\s*(?:static\s+)?(?:void|int|String|boolean|\w+)\s+\w+\s*\(", stripped):
                    match = re.search(r"\s+(\w+)\s*\(", stripped)
                    structure["functions"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })

            elif ext == ".go":
                if re.match(r"^import\s+", stripped) or re.match(r"^import\s*\(", stripped):
                    structure["imports"].append({"line": i + 1, "content": stripped[:80]})
                elif re.match(r"^type\s+\w+\s+struct", stripped):
                    match = re.match(r"^type\s+(\w+)", stripped)
                    structure["classes"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })
                elif re.match(r"^func\s+", stripped):
                    match = re.match(r"^func\s+(?:\([^)]+\)\s*)?(\w+)", stripped)
                    structure["functions"].append({
                        "line": i + 1,
                        "name": match.group(1) if match else "unknown",
                        "content": stripped[:80]
                    })

        summary = f"文件 {path} ({len(lines)} 行)\n"
        if structure["imports"]:
            summary += f"导入 ({len(structure['imports'])}个): {', '.join([i['content'][:30] for i in structure['imports'][:5]])}\n"
        if structure["classes"]:
            summary += f"类 ({len(structure['classes'])}个): {', '.join([c['name'] for c in structure['classes']])}\n"
        if structure["functions"]:
            summary += f"函数 ({len(structure['functions'])}个): {', '.join([f['name'] for f in structure['functions'][:10]])}\n"

        return json.dumps({
            "success": True,
            "path": path,
            "total_lines": len(lines),
            "structure": structure,
            "summary": summary,
            "hint": "根据结构定位关键代码，使用 file_operation 工具的 offset/limit 参数读取特定部分"
        }, ensure_ascii=False)

    def _search_pattern(self, path: str, lines: List[str], pattern: str, ext: str) -> str:
        if not pattern:
            return json.dumps({"success": False, "error": "search模式需要pattern参数"}, ensure_ascii=False)

        matches = []
        regex = None
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except:
            pass

        for i, line in enumerate(lines):
            stripped = line.strip()
            if pattern.lower() in stripped.lower() or (regex and regex.search(stripped)):
                matches.append({
                    "line": i + 1,
                    "content": stripped[:100]
                })

        return json.dumps({
            "success": True,
            "path": path,
            "pattern": pattern,
            "matches": matches[:20],
            "total_matches": len(matches),
            "hint": f"找到 {len(matches)} 处匹配，使用 file_operation 工具读取相关行"
        }, ensure_ascii=False)