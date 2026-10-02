"""仓库符号地图工具 — 用 tree-sitter 抽取各文件的定义，生成紧凑的“代码地图”。

用途：让模型快速了解项目结构与关键符号位置（类似各编码 agent 的 repo map / codebase
overview），避免盲目通读文件。tree-sitter 不可用时回退正则抽取（近似）。

输出为紧凑文本（文件 + 行号 + 符号），并附结构化 `files` 便于程序消费。
"""
import json
import logging
import os
import re
import time

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

try:  # 复用 code_search 的 tree-sitter 设施（可选依赖）
    from .code_search import (
        LANGUAGE_MAP,
        LANGUAGE_QUERIES,
        _get_ts_language,
        _get_ts_parser,
    )
except Exception:  # noqa: BLE001
    LANGUAGE_MAP = {}
    LANGUAGE_QUERIES = {}

    def _get_ts_language(_ext):  # type: ignore
        return None

    def _get_ts_parser(_ext):  # type: ignore
        return None

try:
    from agent.ignore import AgentIgnore
except Exception:  # noqa: BLE001
    AgentIgnore = None

_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".agent",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build",
    ".next", "target", "out", ".idea", ".vscode", "coverage",
}

# 正则兜底：扩展名 → [(regex, kind)]
_REGEX_PATTERNS = {
    ".py": [(r"^\s*(?:async\s+)?def\s+(\w+)", "def"), (r"^\s*class\s+(\w+)", "class")],
    ".js": [(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
            (r"^\s*(?:export\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function)", "function")],
    ".ts": [(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
            (r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
            (r"^\s*(?:export\s+)?interface\s+(\w+)", "interface"),
            (r"^\s*(?:export\s+)?type\s+(\w+)", "type"),
            (r"^\s*(?:export\s+)?enum\s+(\w+)", "enum"),
            (r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function)", "function")],
    ".go": [(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)", "func"), (r"^\s*type\s+(\w+)", "type")],
    ".rs": [(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", "fn"),
            (r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)", "type"),
            (r"^\s*impl\s+(\w+)", "impl")],
    ".java": [(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?class\s+(\w+)", "class"),
              (r"^\s*(?:public|private|protected)?\s*interface\s+(\w+)", "interface")],
    ".kt": [(r"^\s*(?:fun\s+)(\w+)", "fun"), (r"^\s*(?:class|object|interface)\s+(\w+)", "class")],
    ".rb": [(r"^\s*def\s+(\w+)", "def"), (r"^\s*(?:class|module)\s+(\w+)", "class")],
    ".php": [(r"^\s*(?:public|private|protected)?\s*function\s+(\w+)", "function"),
             (r"^\s*class\s+(\w+)", "class")],
    ".c": [(r"^\s*(?:static\s+)?[\w\*\s]+\s+(\w+)\s*\([^;]*\)\s*\{", "function")],
}
for _e in (".jsx", ".tsx", ".mjs", ".cjs"):
    _REGEX_PATTERNS[_e] = _REGEX_PATTERNS[".ts"]
for _e in (".cc", ".cpp", ".cxx", ".h", ".hpp"):
    _REGEX_PATTERNS[_e] = _REGEX_PATTERNS[".c"]

_SUPPORTED_EXT = set(LANGUAGE_MAP) | set(_REGEX_PATTERNS)

# 简单 TTL 缓存：(workspace, path, max_files, max_symbols) -> (expires, result)
_CACHE: dict[tuple, tuple[float, dict]] = {}
_CACHE_TTL = 20.0


def _regex_symbols(path: str, ext: str) -> list[tuple[str, str, int]]:
    patterns = _REGEX_PATTERNS.get(ext)
    if not patterns:
        return []
    out: list[tuple[str, str, int]] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                for regex, kind in patterns:
                    m = re.match(regex, line)
                    if m:
                        out.append((kind, m.group(1), i))
                        break
    except Exception:  # noqa: BLE001
        return []
    return out


def _ts_symbols(path: str, ext: str) -> list[tuple[str, str, int]]:
    lang = _get_ts_language(ext)
    parser = _get_ts_parser(ext)
    if not lang or not parser:
        return []
    queries = LANGUAGE_QUERIES.get(LANGUAGE_MAP.get(ext), [])
    if not queries:
        return []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            code = f.read()
        tree = parser.parse(bytes(code, "utf-8"))
        out: list[tuple[str, str, int]] = []
        for qdef in queries:
            if qdef.get("kind") == "call":
                continue
            try:
                q = lang.query(qdef["query"])
                for node, _cap in q.captures(tree.root_node):
                    name = node.text.decode("utf-8") if node.text else ""
                    if name:
                        out.append((qdef["kind"], name, node.start_point[0] + 1))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug(f"repo_map tree-sitter 解析失败 {path}: {e}")
        return []


def _symbols_for(path: str, ext: str) -> list[tuple[str, str, int]]:
    symbols = _ts_symbols(path, ext) or _regex_symbols(path, ext)
    # 去重 + 按行排序
    seen = set()
    uniq = []
    for kind, name, line in symbols:
        key = (kind, name, line)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((kind, name, line))
    uniq.sort(key=lambda x: (x[2], x[1]))
    return uniq


class RepoMapTool(BuiltinTool):
    """生成仓库符号地图（文件 + 定义符号 + 行号）。"""

    @property
    def name(self) -> str:
        return "repo_map"

    @property
    def description(self) -> str:
        return (
            "生成当前工作区的**符号地图**：用 tree-sitter 抽取各文件的类/函数/方法等定义"
            "（含行号），快速了解项目结构与关键符号位置；避免盲目通读文件。"
            "适合开始编码任务时先看整体结构。tree-sitter 不可用时回退正则（近似）。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "子目录（相对工作区；缺省整仓）"},
                "max_files": {"type": "integer", "description": "最多文件数（默认 200）", "default": 200},
                "max_symbols": {"type": "integer", "description": "每文件最多符号数（默认 25）", "default": 25},
            },
        }

    async def execute(self, path: str = "", max_files: int = 200, max_symbols: int = 25, **kwargs) -> str:
        workspace = self.workspace or os.getcwd()
        root = self.resolve_path(path) if path else workspace
        if not os.path.isdir(root):
            return self._error(f"目录不存在: {path or root}")

        try:
            max_files = max(1, min(int(max_files or 200), 1000))
            max_symbols = max(1, min(int(max_symbols or 25), 200))
        except (TypeError, ValueError):
            max_files, max_symbols = 200, 25

        cache_key = (workspace, root, max_files, max_symbols)
        hit = _CACHE.get(cache_key)
        now = time.time()
        if hit and hit[0] > now:
            return json.dumps(hit[1], ensure_ascii=False)

        ignore = AgentIgnore(workspace) if AgentIgnore else None
        files: list[dict] = []
        lines: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in sorted(filenames):
                if len(files) >= max_files:
                    break
                full = os.path.join(dirpath, name)
                ext = os.path.splitext(name)[1].lower()
                if ext not in _SUPPORTED_EXT:
                    continue
                rel = os.path.relpath(full, workspace).replace("\\", "/")
                if ignore and ignore.should_ignore(rel):
                    continue
                symbols = _symbols_for(full, ext)
                if not symbols:
                    continue
                shown = symbols[:max_symbols]
                files.append({
                    "path": rel,
                    "symbol_count": len(symbols),
                    "symbols": [{"kind": k, "name": n, "line": ln} for k, n, ln in shown],
                })
                block = [f"{rel}:"]
                for k, n, ln in shown:
                    block.append(f"  {ln}\t{k} {n}")
                if len(symbols) > len(shown):
                    block.append(f"  …(+{len(symbols) - len(shown)})")
                lines.append("\n".join(block))
            if len(files) >= max_files:
                break

        result = {
            "success": True,
            "root": os.path.relpath(root, workspace).replace("\\", "/") if root != workspace else ".",
            "file_count": len(files),
            "symbol_count": sum(f["symbol_count"] for f in files),
            "truncated": len(files) >= max_files,
            "map": "\n".join(lines),
            "files": files,
        }
        try:
            _CACHE[cache_key] = (now + _CACHE_TTL, result)
            for k in [k for k, v in _CACHE.items() if v[0] <= now]:
                _CACHE.pop(k, None)
        except Exception:  # noqa: BLE001
            pass
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
