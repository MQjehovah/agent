import datetime
import fnmatch
import logging
import os
import shutil
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("mcp.filesystem")

mcp = MCPServer("Rosiwit MCP Server")

DEFAULT_MAX_READ_BYTES = 512 * 1024
MAX_LIST_DEPTH = 5
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
SEARCH_MAX_FILE_BYTES = 128 * 1024
SEARCH_MAX_TOTAL_BYTES = 2 * 1024 * 1024

TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".ndjson",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".vue", ".html", ".htm", ".css", ".scss",
    ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".sh", ".bash", ".ps1", ".bat", ".cmd", ".sql", ".xml", ".svg", ".gradle", ".dockerfile",
})

_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)
_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


class FsError(Exception):
    """受限目录操作失败, message 为可读中文原因。"""


ROOTS_NOT_CONFIGURED = (
    "未配置受限目录: 请通过环境变量 FS_MCP_ROOTS 指定允许访问的绝对路径(逗号分隔)后重启服务"
)


def _ensure_roots_configured() -> None:
    if not _roots():
        raise FsError(ROOTS_NOT_CONFIGURED)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _roots() -> list[Path]:
    """受限目录白名单, 来自 FS_MCP_ROOTS(逗号分隔绝对路径); 默认空=不可用。"""
    roots: list[Path] = []
    for part in os.getenv("FS_MCP_ROOTS", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).expanduser().resolve())
        except OSError:
            logger.warning(f"忽略无法解析的受限目录: {part}")
    return roots


def _write_allowed() -> bool:
    return os.getenv("FS_MCP_ALLOW_WRITE", "false").strip().lower() in ("1", "true", "yes", "on")


def _max_read_bytes() -> int:
    return _env_int("FS_MCP_MAX_READ_BYTES", DEFAULT_MAX_READ_BYTES)


def resolve_in_roots(path: str, roots: list[Path] | None = None) -> Path:
    """把 path 规范化并解析符号链接后, 校验其位于受限目录内。

    相对路径基于第一个 root 解析; 绝对路径也必须落在某个 root 内。`..` 与符号链接
    逃逸都会被 Path.resolve() 展开后拒绝。失败抛 FsError(可读中文)。
    """
    roots = _roots() if roots is None else roots
    if not roots:
        raise FsError(ROOTS_NOT_CONFIGURED)
    raw = str(path or "").strip() or "."
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise FsError(f"路径解析失败: {path!r} ({exc})") from exc
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise FsError(f"路径越界: {path!r} 不在允许的受限目录内")


def _require_write() -> None:
    """写操作前置检查: 受限目录必须已配置, 且写开关已打开。"""
    _ensure_roots_configured()
    if not _write_allowed():
        raise FsError("写操作未开启: 需设置环境变量 FS_MCP_ALLOW_WRITE=true 并重启服务")


def _rel(target: Path, base: Path) -> str:
    try:
        rel = target.relative_to(base)
    except ValueError:
        return str(target)
    return "." if str(rel) == "." else str(rel)


def _entry_type(target: Path) -> dict:
    entry = {"name": target.name}
    try:
        is_link = target.is_symlink()
        is_dir = target.is_dir()
        entry["type"] = "dir" if is_dir else "file"
        entry["symlink"] = is_link
        entry["size"] = None if is_dir else target.stat().st_size
    except OSError as exc:
        entry["type"] = "error"
        entry["symlink"] = False
        entry["size"] = None
        entry["error"] = str(exc)
    return entry


def _fs_list(path: str, depth: int) -> dict:
    base = resolve_in_roots(path)
    if not base.exists():
        raise FsError(f"路径不存在: {path!r}")
    try:
        depth = max(1, min(int(depth or 1), MAX_LIST_DEPTH))
    except (TypeError, ValueError):
        depth = 1

    entries: list[dict] = []
    truncated = False

    def walk(current: Path, level: int) -> None:
        nonlocal truncated
        if truncated:
            return
        try:
            children = sorted(current.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        except OSError as exc:
            raise FsError(f"目录读取失败: {current} ({exc})") from exc
        for child in children:
            if len(entries) >= MAX_LIST_ENTRIES:
                truncated = True
                return
            entry = _entry_type(child)
            entry["path"] = _rel(child, base)
            entries.append(entry)
            if entry.get("type") == "dir" and not entry.get("symlink") and level < depth:
                walk(child, level + 1)

    if base.is_file():
        entry = _entry_type(base)
        entry["path"] = base.name
        entries.append(entry)
    else:
        walk(base, 1)

    return {
        "path": str(base),
        "depth": depth,
        "count": len(entries),
        "truncated": truncated,
        "entries": entries,
    }


def _fs_read_text(path: str, max_bytes: int) -> dict:
    limit = _max_read_bytes()
    try:
        requested = int(max_bytes)
    except (TypeError, ValueError):
        requested = 0
    if requested > 0:
        limit = min(requested, limit)

    target = resolve_in_roots(path)
    if not target.is_file():
        raise FsError(f"不是可读文件: {path!r}")
    size = target.stat().st_size
    with open(target, "rb") as handle:
        data = handle.read(limit + 1)
    truncated = len(data) > limit
    data = data[:limit]
    return {
        "path": str(target),
        "size": size,
        "limit_bytes": limit,
        "returned_bytes": len(data),
        "truncated": truncated,
        "content": data.decode("utf-8", errors="replace"),
    }


def _fs_stat(path: str) -> dict:
    target = resolve_in_roots(path)
    if not target.exists() and not target.is_symlink():
        raise FsError(f"路径不存在: {path!r}")
    stat = target.stat()
    is_dir = target.is_dir()
    return {
        "path": str(target),
        "name": target.name,
        "type": "dir" if is_dir else "file",
        "symlink": target.is_symlink(),
        "suffix": target.suffix,
        "size": stat.st_size,
        "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
    }


def _fs_search(path: str, pattern: str, max_results: int) -> dict:
    base = resolve_in_roots(path)
    if not base.is_dir():
        raise FsError(f"不是目录: {path!r}")
    keyword = str(pattern or "").strip()
    if not keyword:
        raise FsError("pattern 不能为空")
    try:
        limit = max(1, min(int(max_results or 50), MAX_SEARCH_RESULTS))
    except (TypeError, ValueError):
        limit = 50

    lowered = keyword.lower()
    matches: list[dict] = []
    scanned_bytes = 0
    truncated = False

    for root, dirs, files in os.walk(base, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            if len(matches) >= limit:
                truncated = True
                break
            target = Path(root) / name
            relative = _rel(target, base)
            if target.is_symlink():
                continue
            if fnmatch.fnmatch(name.lower(), lowered) or lowered in name.lower():
                matches.append({"path": relative, "kind": "name"})
                continue
            if target.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if scanned_bytes >= SEARCH_MAX_TOTAL_BYTES:
                truncated = True
                break
            try:
                with open(target, "rb") as handle:
                    raw = handle.read(SEARCH_MAX_FILE_BYTES)
            except OSError:
                continue
            scanned_bytes += len(raw)
            text = raw.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if lowered in line.lower():
                    matches.append({
                        "path": relative,
                        "kind": "content",
                        "line": line_number,
                        "text": line.strip()[:200],
                    })
                    break
        if truncated:
            break

    return {
        "path": str(base),
        "pattern": pattern,
        "count": len(matches),
        "truncated": truncated,
        "scanned_bytes": scanned_bytes,
        "matches": matches,
    }


def _fs_write_text(path: str, content: str, create_dirs: bool) -> dict:
    _require_write()
    target = resolve_in_roots(path)
    if target.exists() and target.is_dir():
        raise FsError(f"目标是目录, 不能写入: {path!r}")
    parent = target.parent
    if not parent.exists():
        if create_dirs:
            parent.mkdir(parents=True, exist_ok=True)
        else:
            raise FsError(f"父目录不存在: {parent}; 可传 create_dirs=true 自动创建")
    data = str(content if content is not None else "").encode("utf-8")
    target.write_bytes(data)
    return {"success": True, "path": str(target), "bytes": len(data)}


def _fs_mkdir(path: str) -> dict:
    _require_write()
    target = resolve_in_roots(path)
    if target.exists():
        if target.is_dir():
            return {"success": True, "path": str(target), "created": False}
        raise FsError(f"同名文件已存在: {path!r}")
    target.mkdir(parents=True, exist_ok=True)
    return {"success": True, "path": str(target), "created": True}


def _fs_move(src: str, dst: str) -> dict:
    _require_write()
    source = resolve_in_roots(src)
    destination = resolve_in_roots(dst)
    if source in _roots():
        raise FsError("拒绝移动受限目录根")
    if not source.exists() and not source.is_symlink():
        raise FsError(f"源路径不存在: {src!r}")
    if destination.exists() or destination.is_symlink():
        raise FsError(f"目标已存在: {dst!r}")
    if not destination.parent.exists():
        raise FsError(f"目标父目录不存在: {destination.parent}")
    shutil.move(str(source), str(destination))
    return {"success": True, "src": str(source), "dst": str(destination)}


def _fs_delete(path: str, recursive: bool) -> dict:
    _require_write()
    target = resolve_in_roots(path)
    if target in _roots():
        raise FsError("拒绝删除受限目录根")
    if not target.exists() and not target.is_symlink():
        raise FsError(f"路径不存在: {path!r}")
    if target.is_dir() and not target.is_symlink():
        if not recursive:
            raise FsError("目标是目录, 删除需 recursive=true")
        shutil.rmtree(target)
        kind = "dir"
    else:
        target.unlink()
        kind = "file"
    return {"success": True, "path": str(target), "kind": kind}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def fs_list(path: str = ".", depth: int = 1) -> dict:
    """列出受限目录下的文件/子目录(只读)。

    参数:
    - path: 相对路径(基于第一个 FS_MCP_ROOTS)或 root 内的绝对路径, 默认 "."
    - depth: 递归深度(1=仅直接子项, 上限 5), 默认 1

    返回 path/depth/count/truncated(超过 500 条截断)/entries(含 name/type/symlink/size/path)。
    未配置 FS_MCP_ROOTS 或路径越界时返回 {"error": "..."}。
    """
    logger.info(f"fs_list: {path} depth={depth}")
    try:
        return _fs_list(path, depth)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"目录读取失败: {exc}"}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def fs_read_text(path: str, max_bytes: int = 0) -> dict:
    """读取受限目录内的文本文件(只读)。

    参数:
    - path: 相对路径或 root 内绝对路径
    - max_bytes: 本次最多读取的字节数, 0 表示使用 FS_MCP_MAX_READ_BYTES(默认 512KB);
      请求值会被上限截断, 不允许超过配置值

    返回 path/size/limit_bytes/returned_bytes/truncated/content(UTF-8, 非法字节以 U+FFFD 替换)。
    未配置 roots、路径越界、不是文件或读取失败时返回 {"error": "..."}。
    """
    logger.info(f"fs_read_text: {path} max_bytes={max_bytes}")
    try:
        return _fs_read_text(path, max_bytes)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"文件读取失败: {exc}"}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def fs_stat(path: str) -> dict:
    """查看受限目录内路径的元信息(只读)。

    返回 path/name/type(file|dir)/symlink/suffix/size/mtime(ISO8601)。
    未配置 roots、路径越界或不存在时返回 {"error": "..."}。
    """
    logger.info(f"fs_stat: {path}")
    try:
        return _fs_stat(path)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"状态读取失败: {exc}"}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def fs_search(path: str, pattern: str, max_results: int = 50) -> dict:
    """在受限目录内按文件名/内容搜索(只读)。

    参数:
    - path: 搜索根目录(相对路径或 root 内绝对路径)
    - pattern: 关键词, 不区分大小写; 文件名匹配优先, 内容匹配仅扫描文本后缀文件
    - max_results: 最多返回条数(上限 200), 默认 50

    内容扫描总预算 2MB(单文件最多 128KB), 符号链接文件跳过不读。
    返回 path/pattern/count/truncated/scanned_bytes/matches(kind=name|content, content 含 line/text)。
    """
    logger.info(f"fs_search: {path} pattern={pattern!r}")
    try:
        return _fs_search(path, pattern, max_results)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"搜索失败: {exc}"}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def fs_write_text(path: str, content: str, create_dirs: bool = False) -> dict:
    """写入文本文件(写操作, 默认关闭)。

    需同时满足: FS_MCP_ROOTS 已配置且路径在内、FS_MCP_ALLOW_WRITE=true。
    - path: 相对路径或 root 内绝对路径
    - content: 文本内容(UTF-8 写入, 覆盖同名文件)
    - create_dirs: 父目录不存在时是否自动创建, 默认 false

    返回 success/path/bytes; 未开启写、越界或写失败时返回 {"error": "..."}。
    """
    logger.info(f"fs_write_text: {path} ({len(str(content or ''))} chars)")
    try:
        return _fs_write_text(path, content, create_dirs)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"写入失败: {exc}"}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def fs_mkdir(path: str) -> dict:
    """创建目录(写操作, 默认关闭; 需 FS_MCP_ALLOW_WRITE=true)。

    父目录会自动创建; 目录已存在时返回 created=false 而不报错。
    """
    logger.info(f"fs_mkdir: {path}")
    try:
        return _fs_mkdir(path)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"创建目录失败: {exc}"}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def fs_move(src: str, dst: str) -> dict:
    """移动/重命名文件或目录(写操作, 默认关闭; 需 FS_MCP_ALLOW_WRITE=true)。

    src 与 dst 都必须位于受限目录内, 目标已存在时拒绝覆盖。
    """
    logger.info(f"fs_move: {src} -> {dst}")
    try:
        return _fs_move(src, dst)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"移动失败: {exc}"}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def fs_delete(path: str, recursive: bool = False) -> dict:
    """删除文件或目录(写操作, 默认关闭; 需 FS_MCP_ALLOW_WRITE=true)。

    目录必须显式传 recursive=true; 受限目录根本身拒绝删除。
    """
    logger.info(f"fs_delete: {path} recursive={recursive}")
    try:
        return _fs_delete(path, recursive)
    except FsError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"删除失败: {exc}"}


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
