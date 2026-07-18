"""
结构化代码搜索工具 — 多语言 AST 搜索

支持:
- 查找函数/类定义（基于 Tree-sitter AST）
- 查找调用方
- 查找引用
- 跨文件分析

支持语言:
  Python (.py), JavaScript (.js,.jsx), TypeScript (.ts,.tsx),
  Go (.go), Rust (.rs), Java (.java), Kotlin (.kt),
  C (.c,.h), C++ (.cpp,.hpp), C# (.cs),
  Ruby (.rb), PHP (.php), Swift (.swift), Scala (.scala)

用法:
    # 查找定义
    code_search(query="get_user", target="definition")

    # 查找调用方
    code_search(query="get_user", target="callers")

    # 全面分析
    code_search(query="get_user", target="all")
"""
import json
import logging
import os
import re
import subprocess

from tools import BuiltinTool

logger = logging.getLogger("agent.tools.code_search")

# ── Tree-sitter 初始化（延迟加载） ─────────────────────

_ts_available = False
_ts_dll_path = None


def _init_tree_sitter():
    global _ts_available, _ts_dll_path
    if _ts_available:
        return True
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            import tree_sitter_languages
        _ts_dll_path = os.path.join(
            os.path.dirname(tree_sitter_languages.__file__), "languages.dll"
        )
        if os.path.exists(_ts_dll_path):
            _ts_available = True
            return True
    except Exception as e:
        logger.debug(f"tree-sitter 不可用: {e}")
    return False


def _get_ts_language(ext: str):
    """获取 Tree-sitter Language 对象，失败返回 None"""
    lang_name = LANGUAGE_MAP.get(ext)
    if not lang_name or not _init_tree_sitter():
        return None
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            from tree_sitter import Language
            return Language(_ts_dll_path, lang_name)
    except Exception as e:
        logger.debug(f"加载 tree-sitter 语言 [{lang_name}] 失败: {e}")
        return None


def _get_ts_parser(ext: str):
    """获取 Tree-sitter Parser 对象，失败返回 None"""
    lang = _get_ts_language(ext)
    if not lang:
        return None
    try:
        from tree_sitter import Parser
        parser = Parser()
        parser.set_language(lang)
        return parser
    except Exception as e:
        logger.debug(f"创建 tree-sitter parser 失败: {e}")
        return None


# ── 语言映射 ────────────────────────────────────────

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".swift": "swift",
    ".scala": "scala",
}

# ── 语言特定的 AST 查询表达式（Tree-sitter S-expressions） ──

# 每个语言定义多组查询：
#   name: 返回的名称字段
#   kind: 结果类型标签
#   query: S-expression 字符串

LANGUAGE_QUERIES = {
    "python": [
        {"name": "function_def", "query": "(function_definition name: (identifier) @name)", "kind": "function"},
        {"name": "async_function_def", "query": "(async_function_definition name: (identifier) @name)", "kind": "async_function"},
        {"name": "class_def", "query": "(class_definition name: (identifier) @name)", "kind": "class"},
        {"name": "decorated_function", "query": "(decorated_definition (function_definition name: (identifier) @name))", "kind": "function"},
        {"name": "call", "query": "(call function: (identifier) @caller)", "kind": "call"},
    ],
    "javascript": [
        {"name": "function_decl", "query": "(function_declaration name: (identifier) @name)", "kind": "function"},
        {"name": "arrow_function", "query": "(arrow_function name: (identifier) @name)", "kind": "function"},
        {"name": "class_decl", "query": "(class_declaration name: (identifier) @name)", "kind": "class"},
        {"name": "method_def", "query": "(method_definition name: (property_identifier) @name)", "kind": "method"},
        {"name": "call", "query": "(call_expression function: (identifier) @caller)", "kind": "call"},
        {"name": "export_func", "query": "(export_statement (function_declaration name: (identifier) @name))", "kind": "function"},
    ],
    "typescript": [
        {"name": "function_decl", "query": "(function_declaration name: (identifier) @name)", "kind": "function"},
        {"name": "arrow_function", "query": "(arrow_function name: (identifier) @name)", "kind": "function"},
        {"name": "class_decl", "query": "(class_declaration name: (type_identifier) @name)", "kind": "class"},
        {"name": "method_def", "query": "(method_definition name: (property_identifier) @name)", "kind": "method"},
        {"name": "interface_decl", "query": "(interface_declaration name: (type_identifier) @name)", "kind": "interface"},
        {"name": "call", "query": "(call_expression function: (identifier) @caller)", "kind": "call"},
    ],
    "go": [
        {"name": "func_decl", "query": "(function_declaration name: (identifier) @name)", "kind": "function"},
        {"name": "method_decl", "query": "(method_declaration name: (field_identifier) @name)", "kind": "method"},
        {"name": "type_struct", "query": "(type_declaration (type_spec name: (type_identifier) @name))", "kind": "struct"},
        {"name": "type_interface", "query": "(type_declaration (type_spec name: (type_identifier) @name (interface_type)))", "kind": "interface"},
        {"name": "call", "query": "(call_expression function: (identifier) @caller)", "kind": "call"},
    ],
    "rust": [
        {"name": "fn_item", "query": "(function_item name: (identifier) @name)", "kind": "function"},
        {"name": "struct_item", "query": "(struct_item name: (type_identifier) @name)", "kind": "struct"},
        {"name": "impl_item", "query": "(impl_item trait: (type_identifier) @name)", "kind": "impl"},
        {"name": "trait_item", "query": "(trait_item name: (type_identifier) @name)", "kind": "trait"},
        {"name": "enum_item", "query": "(enum_item name: (type_identifier) @name)", "kind": "enum"},
        {"name": "call", "query": "(call_expression function: (identifier) @caller)", "kind": "call"},
    ],
    "java": [
        {"name": "method_decl", "query": "(method_declaration name: (identifier) @name)", "kind": "method"},
        {"name": "class_decl", "query": "(class_declaration name: (identifier) @name)", "kind": "class"},
        {"name": "interface_decl", "query": "(interface_declaration name: (identifier) @name)", "kind": "interface"},
        {"name": "call", "query": "(method_invocation name: (identifier) @caller)", "kind": "call"},
    ],
    "kotlin": [
        {"name": "function", "query": "(function_declaration name: (simple_identifier) @name)", "kind": "function"},
        {"name": "class", "query": "(class_declaration name: (simple_identifier) @name)", "kind": "class"},
    ],
    "ruby": [
        {"name": "method", "query": "(method name: (identifier) @name)", "kind": "method"},
        {"name": "class", "query": "(class name: (constant) @name)", "kind": "class"},
        {"name": "call", "query": "(call method: (identifier) @caller)", "kind": "call"},
    ],
    "php": [
        {"name": "function", "query": "(function_definition name: (name) @name)", "kind": "function"},
        {"name": "class", "query": "(class_declaration name: (name) @name)", "kind": "class"},
        {"name": "method", "query": "(method_declaration name: (name) @name)", "kind": "method"},
    ],
    "c": [
        {"name": "function", "query": "(function_definition declarator: (function_declarator declarator: (identifier) @name))", "kind": "function"},
        {"name": "call", "query": "(call_expression function: (identifier) @caller)", "kind": "call"},
    ],
    "cpp": [
        {"name": "function", "query": "(function_definition declarator: (function_declarator declarator: (identifier) @name))", "kind": "function"},
        {"name": "class", "query": "(class_specifier name: (type_identifier) @name)", "kind": "class"},
        {"name": "call", "query": "(call_expression function: (identifier) @caller)", "kind": "call"},
    ],
}


class CodeSearchTool(BuiltinTool):
    """结构化代码搜索（多语言 AST）"""

    @property
    def name(self) -> str:
        return "code_search"

    @property
    def description(self) -> str:
        return """结构化代码搜索工具（基于 Tree-sitter AST）。能理解代码结构，区分定义和调用。

【和 grep 的区别】
- code_search: 基于 AST 的结构化搜索，支持 Python/JS/TS/Go/Rust/Java 等主流语言，
  能精确识别函数定义、类定义、方法、调用方
- grep: 基于正则表达式的全文搜索，**支持所有文件类型**

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
                    "enum": ["function", "class", "method", "variable", "all"],
                    "description": "符号类型过滤（可选，仅 AST 支持的语言有效）"
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
            defs = self._find_definitions(query, workspace, file_path, symbol_type)
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

    # ── Tree-sitter AST 解析 ─────────────────────────

    def _parse_with_ts(self, file_path: str) -> tuple | None:
        """用 Tree-sitter 解析文件，返回 (tree, lang_obj, ext, code) 或 None"""
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in LANGUAGE_MAP:
            return None
        lang = _get_ts_language(ext)
        if not lang:
            return None
        parser = _get_ts_parser(ext)
        if not parser:
            return None
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                code = f.read()
            tree = parser.parse(bytes(code, "utf-8"))
            return tree, lang, ext, code
        except Exception as e:
            logger.debug(f"Tree-sitter 解析失败 {file_path}: {e}")
            return None

    def _ts_find_definitions(self, query: str, file_path: str,
                              symbol_type: str, lang, tree, code: str) -> list[dict]:
        """在 Tree-sitter AST 中查找定义"""
        ext = os.path.splitext(file_path)[1].lower()
        lang_name = LANGUAGE_MAP.get(ext)
        queries = LANGUAGE_QUERIES.get(lang_name, [])
        if not queries:
            return []

        results = []
        root = tree.root_node
        def_queries = [q for q in queries if q["kind"] != "call"]

        for qdef in def_queries:
            if symbol_type != "all" and qdef["kind"] != symbol_type:
                continue
            try:
                q = lang.query(qdef["query"])
                for node, cap_name in q.captures(root):
                    name = node.text.decode("utf-8") if node.text else ""
                    if name == query:
                        start_row, start_col = node.start_point
                        results.append({
                            "kind": qdef["kind"],
                            "name": name,
                            "file": file_path,
                            "line": start_row + 1,
                            "column": start_col,
                        })
            except Exception as e:
                logger.debug(f"Tree-sitter 查询 [{qdef['name']}] 失败: {e}")

        return results

    def _ts_find_callers(self, query: str, file_path: str,
                          lang, tree, code: str) -> list[dict]:
        """在 Tree-sitter AST 中查找调用方"""
        ext = os.path.splitext(file_path)[1].lower()
        lang_name = LANGUAGE_MAP.get(ext)
        queries = LANGUAGE_QUERIES.get(lang_name, [])
        if not queries:
            return []

        results = []
        root = tree.root_node
        lines = code.split("\n")
        call_queries = [q for q in queries if q["kind"] == "call"]

        for qdef in call_queries:
            try:
                q = lang.query(qdef["query"])
                for node, cap_name in q.captures(root):
                    name = node.text.decode("utf-8") if node.text else ""
                    if name == query:
                        start_row, start_col = node.start_point
                        line_text = lines[start_row] if start_row < len(lines) else ""
                        results.append({
                            "file": file_path,
                            "line": start_row + 1,
                            "code": line_text.strip()[:200],
                        })
            except Exception as e:
                logger.debug(f"Tree-sitter 调用方查询失败: {e}")

        return results

    # ── 主查找方法 ───────────────────────────────────

    def _find_definitions(self, query: str, workspace: str,
                           file_path: str = "", symbol_type: str = "all") -> list[dict]:
        """查找定义：优先使用 Tree-sitter，回退到 grep"""
        definitions = []

        # 确定搜索范围
        files = self._grep_files_for_def(query, workspace, file_path)
        if not files:
            return definitions

        for file in files[:10]:
            full_path = file if os.path.isabs(file) else os.path.join(workspace, file)
            if not os.path.isfile(full_path):
                continue

            # 尝试 Tree-sitter AST 解析
            ts_result = self._parse_with_ts(full_path)
            if ts_result:
                tree, lang, ext, code = ts_result
                defs = self._ts_find_definitions(query, full_path, symbol_type, lang, tree, code)
                if defs:
                    definitions.extend(defs)
                    continue

            # 回退：行级 grep
            ext = os.path.splitext(full_path)[1].lower()
            defs = self._grep_definitions_fallback(query, file, full_path, symbol_type, ext)
            definitions.extend(defs)

        return definitions

    async def _find_callers(self, query: str, workspace: str, file_path: str = "") -> list[dict]:
        """查找调用方：优先 Tree-sitter，回退到 grep"""
        callers = []
        files = self._grep_files(re.escape(query), workspace, file_path)

        for file in files[:15]:
            full_path = file if os.path.isabs(file) else os.path.join(workspace, file)
            if not os.path.isfile(full_path):
                continue

            # 尝试 Tree-sitter
            ts_result = self._parse_with_ts(full_path)
            if ts_result:
                tree, lang, ext, code = ts_result
                ts_callers = self._ts_find_callers(query, full_path, lang, tree, code)
                if ts_callers:
                    callers.extend(ts_callers)
                    continue

            # 回退：grep 行级匹配
            lines = self._get_matching_lines(full_path, query)
            for line_no, line_text in lines:
                if re.match(rf"\s*(class |def |async def |function |func |fn ){re.escape(query)}", line_text):
                    continue
                callers.append({
                    "file": file,
                    "line": line_no,
                    "code": line_text.strip()[:200],
                })

        return callers

    async def _find_references(self, query: str, workspace: str, file_path: str = "") -> list[dict]:
        """查找所有引用（基于 grep，因为引用范围最广）"""
        refs = []
        files = self._grep_files(re.escape(query), workspace, file_path)

        for file in files[:20]:
            full_path = file if os.path.isabs(file) else os.path.join(workspace, file)
            if not os.path.isfile(full_path):
                continue

            lines = self._get_matching_lines(full_path, query)
            for line_no, line_text in lines:
                kind = self._classify_reference(line_text, query)
                refs.append({
                    "file": file,
                    "line": line_no,
                    "code": line_text.strip()[:200],
                    "kind": kind,
                })

        return refs

    # ── grep 辅助 ─────────────────────────────────────

    def _grep_files_for_def(self, query: str, workspace: str, file_path: str = "") -> list[str]:
        """搜索可能包含定义的文件（多语言关键词）"""
        # 用 OR 模式匹配各种语言的定义关键字
        def_keywords = (
            r"(class |def |async def |function |func |fn |"
            r"interface |struct |trait |enum |impl )"
        )
        pattern = def_keywords + re.escape(query)
        return self._grep_files(pattern, workspace, file_path)

    def _grep_files(self, pattern: str, workspace: str, file_path: str = "") -> list[str]:
        """grep 搜索包含匹配的文件"""
        search_path = file_path if file_path else workspace
        if not os.path.exists(search_path):
            return []

        try:
            include_exts = [
                "--include=*.py", "--include=*.ts", "--include=*.tsx",
                "--include=*.js", "--include=*.jsx",
                "--include=*.rs", "--include=*.java", "--include=*.go",
                "--include=*.kt", "--include=*.rb", "--include=*.php",
                "--include=*.c", "--include=*.h", "--include=*.cpp", "--include=*.hpp",
                "--include=*.cs", "--include=*.swift", "--include=*.scala",
                "--include=*.md",
            ]
            exclude_dirs = [
                "--exclude-dir=.git", "--exclude-dir=node_modules",
                "--exclude-dir=.venv", "--exclude-dir=__pycache__",
                "--exclude-dir=target", "--exclude-dir=build",
                "--exclude-dir=dist", "--exclude-dir=vendor",
            ]
            # 选项+模式必须放在路径之前（某些 grep 版本对顺序敏感）
            cmd = (["grep", "-r", "-l", "-E", pattern] + exclude_dirs
                   + include_exts + ["."])
            if os.path.isdir(search_path):
                result = subprocess.run(
                    cmd, cwd=search_path, capture_output=True, text=True, timeout=15,
                )
            else:
                return [search_path]

            files = []
            for f in result.stdout.strip().split("\n"):
                f = f.strip()
                if not f:
                    continue
                # 去掉 grep 返回的 ./ 前缀
                if f.startswith("./"):
                    f = f[2:]
                files.append(f)
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
    def _classify_reference(line_text: str, query: str) -> str:
        """分类引用的类型"""
        if re.match(rf"\s*(class ){re.escape(query)}", line_text):
            return "definition:class"
        if re.match(rf"\s*(def |async def |function |func |fn ){re.escape(query)}", line_text):
            return "definition:function"
        if re.match(rf"\s*(import {re.escape(query)}|from .+ import .*{re.escape(query)})", line_text):
            return "import"
        if re.search(rf"{re.escape(query)}\s*\(", line_text):
            return "call"
        if query in line_text:
            return "reference"
        return "unknown"

    def _grep_definitions_fallback(self, query: str, file_path: str,
                                    full_path: str, symbol_type: str,
                                    ext: str) -> list[dict]:
        """行级 grep 回退方案：支持多语言定义关键词"""
        results = []
        # 按语言匹配对应的定义关键字
        def_patterns = {
            ".py": [("class", r"class\s+(\w+)"), ("function", r"(?:async )?def\s+(\w+)")],
            ".js": [("class", r"class\s+(\w+)"), ("function", r"(?:function|const)\s+(\w+)")],
            ".jsx": [("class", r"class\s+(\w+)"), ("function", r"(?:function|const)\s+(\w+)")],
            ".ts": [("class", r"class\s+(\w+)"), ("function", r"(?:function|const)\s+(\w+)")],
            ".tsx": [("class", r"class\s+(\w+)"), ("function", r"(?:function|const)\s+(\w+)")],
            ".rs": [("struct", r"struct\s+(\w+)"), ("function", r"fn\s+(\w+)")],
            ".go": [("struct", r"type\s+(\w+)\s+struct"), ("function", r"func\s+(?:\([^)]+\)\s*)?(\w+)")],
            ".java": [("class", r"class\s+(\w+)"), ("method", r"(?:public|private|protected).*?(\w+)\s*\(")],
        }
        patterns = def_patterns.get(ext, [("function", r"(\w+)\s*\(")])
        try:
            with open(full_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for i, line in enumerate(lines, 1):
                for kind, pat in patterns:
                    if symbol_type not in ("all", kind):
                        continue
                    m = re.search(pat, line)
                    if m and m.group(1) == query:
                        results.append({
                            "kind": kind,
                            "name": m.group(1),
                            "file": file_path,
                            "line": i,
                            "code": line.strip()[:200],
                        })
        except Exception:
            pass
        return results
