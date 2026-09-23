import logging
import os
import subprocess
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

logger = logging.getLogger("mcp.git")

mcp = MCPServer("Rosiwit MCP Server")

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_OUTPUT_CHARS = 40000
MAX_LOG_LIMIT = 200
BARE_ALLOWED_TOOLS = "git_log/git_show/git_branches"

ROOTS_NOT_CONFIGURED = (
    "未配置仓库目录: 请通过环境变量 GIT_MCP_ROOTS 指定允许访问的仓库绝对路径(逗号分隔)后重启服务"
)
WRITE_DISABLED = "写操作未开启: 需设置环境变量 GIT_MCP_ALLOW_WRITE=true 并重启服务"

_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


class GitError(Exception):
    """受限 Git 操作失败, message 为可读中文原因。"""


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _timeout() -> int:
    """单次 git 命令超时(秒), 环境变量 GIT_MCP_TIMEOUT, 默认 30s。"""
    return _env_int("GIT_MCP_TIMEOUT", DEFAULT_TIMEOUT)


def _max_output_chars() -> int:
    """单次返回输出上限(字符), 环境变量 GIT_MCP_MAX_OUTPUT_CHARS, 默认 40000。"""
    return _env_int("GIT_MCP_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS)


def _roots() -> list[Path]:
    """仓库白名单, 来自 GIT_MCP_ROOTS(逗号分隔绝对路径); 默认空=不可用。"""
    roots: list[Path] = []
    for part in os.getenv("GIT_MCP_ROOTS", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            roots.append(Path(part).expanduser().resolve())
        except OSError:
            logger.warning(f"忽略无法解析的仓库目录: {part}")
    return roots


def _write_allowed() -> bool:
    return os.getenv("GIT_MCP_ALLOW_WRITE", "false").strip().lower() in ("1", "true", "yes", "on")


def _require_write() -> None:
    """写操作前置检查: 仓库白名单必须已配置, 且写开关已打开。"""
    if not _roots():
        raise GitError(ROOTS_NOT_CONFIGURED)
    if not _write_allowed():
        raise GitError(WRITE_DISABLED)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断文本, 返回 (文本, 是否被截断)。"""
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def _decode(data: bytes | str | None) -> str:
    """兼容 bytes/str 两种 subprocess 输出(个别发行版把 Popen 默认设为文本模式)。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def _summarize_stderr(stderr: bytes | str | None) -> str:
    """把 stderr 压缩为单行摘要(最多前 3 行/400 字符), 作为可读错误原因。"""
    text = _decode(stderr).strip()
    if not text:
        return "无错误输出"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[:3])[:400]


def _run_git(repo: Path, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    """在仓库内以 shell=False 执行 git; 超时或无法启动转为可读 GitError。

    子进程环境强制 GIT_TERMINAL_PROMPT=0(禁止交互式输入)、GIT_OPTIONAL_LOCKS=0
    (status 等不抢可选索引锁) 与 LC_ALL=C(错误摘要稳定为英文)。
    """
    limit = timeout or _timeout()
    command = ["git", "--no-pager", "-c", "core.quotepath=false", *args]
    logger.info(f"git {' '.join(args)[:200]} (cwd={repo})")
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LC_ALL": "C",
    }
    try:
        return subprocess.run(command, cwd=str(repo), capture_output=True, timeout=limit, shell=False, env=env)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git 执行超时(>{limit}s): {' '.join(args)}") from exc
    except OSError as exc:
        raise GitError(f"git 执行失败(请确认已安装 git): {exc}") from exc


def _run_checked(repo: Path, args: list[str]) -> str:
    """执行 git 并返回 stdout; 非零退出转为可读 GitError(不抛栈)。"""
    result = _run_git(repo, args)
    if result.returncode != 0:
        raise GitError(f"git 返回 {result.returncode}: {_summarize_stderr(result.stderr)}")
    return _decode(result.stdout)


def _clean_arg(value, label: str) -> str:
    """通用参数清洗: 拒绝换行/NUL 与前导 '-'(防止被当作 git 选项注入)。"""
    text = str(value or "").strip()
    if "\x00" in text or "\n" in text or "\r" in text:
        raise GitError(f"非法{label}: 不允许包含换行或 NUL 字符")
    if text.startswith("-"):
        raise GitError(f"非法{label}: 不允许以 '-' 开头(防选项注入): {text!r}")
    return text


def _require_ref(value, label: str) -> str:
    text = _clean_arg(value, label)
    if not text:
        raise GitError(f"{label} 不能为空")
    return text


def _require_path(value, label: str) -> str:
    """校验仓库内相对路径: 拒绝绝对路径、'..' 逃逸、前导 '-'。"""
    text = _clean_arg(value, label)
    if not text:
        raise GitError(f"{label} 不能为空")
    candidate = Path(text)
    if candidate.is_absolute():
        raise GitError(f"非法{label}: 仅允许仓库内相对路径: {text!r}")
    if ".." in candidate.parts:
        raise GitError(f"非法{label}: 路径不允许包含 '..': {text!r}")
    return text


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_repo(repo: str) -> tuple[Path, bool]:
    """校验 repo 位于 GIT_MCP_ROOTS 内且为仓库根目录, 返回 (绝对路径, 是否裸仓库)。

    相对路径基于第一个 root 解析; resolve() 后做前缀校验, 符号链接逃逸会被拒绝。
    必须为仓库根目录(非子目录): 防止 roots 内子目录指向 roots 之外的 .git 而越权修改。
    非 git 仓库或裸仓库以下工作区工具会被拒绝, 裸仓库仅支持 log/show/branches。
    """
    roots = _roots()
    if not roots:
        raise GitError(ROOTS_NOT_CONFIGURED)
    raw = str(repo or "").strip()
    if not raw:
        raise GitError("repo 不能为空: 需为 GIT_MCP_ROOTS 内的 git 仓库根目录")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise GitError(f"仓库路径解析失败: {repo!r} ({exc})") from exc
    if not any(_is_within(resolved, root) for root in roots):
        raise GitError(f"仓库越界: {repo!r} 不在允许的仓库目录内")
    if not resolved.is_dir():
        raise GitError(f"仓库路径不存在或不是目录: {repo!r}")

    probe = _run_git(resolved, ["rev-parse", "--is-bare-repository"])
    if probe.returncode != 0:
        raise GitError(f"不是有效的 git 仓库: {repo!r} ({_summarize_stderr(probe.stderr)})")
    is_bare = _decode(probe.stdout).strip().lower() == "true"

    root_probe = _run_git(resolved, ["rev-parse", "--absolute-git-dir" if is_bare else "--show-toplevel"])
    expected = _decode(root_probe.stdout).strip()
    if root_probe.returncode != 0 or not expected:
        raise GitError(f"无法解析仓库根目录: {repo!r} ({_summarize_stderr(root_probe.stderr)})")
    try:
        actual_root = Path(expected).resolve()
    except OSError as exc:
        raise GitError(f"仓库根目录解析失败: {expected!r} ({exc})") from exc
    if actual_root != resolved:
        raise GitError(f"repo 必须是仓库根目录: {resolved} 不是 {actual_root}(请传仓库根路径)")
    return resolved, is_bare


def _require_worktree(is_bare: bool, tool: str) -> None:
    if is_bare:
        raise GitError(f"裸仓库不支持工作区操作 {tool}: 裸仓库仅支持 {BARE_ALLOWED_TOOLS}")


def _read_output(repo: Path, args: list[str], extra: dict | None = None) -> dict:
    """执行只读 git 命令并组装统一返回(输出截断到 GIT_MCP_MAX_OUTPUT_CHARS)。"""
    output = _run_checked(repo, args)
    text, truncated = _truncate(output, _max_output_chars())
    payload = {"ok": True, "repo": str(repo)}
    payload.update(extra or {})
    payload.update({"output": text, "truncated": truncated})
    return payload


def _line_range(start_line: int, end_line: int) -> list[str]:
    """把 start_line/end_line 转为 git blame 的 -L 参数; 都为 0 表示全文件。"""
    start = _clamp_int(start_line, 0, 10**9, 0) if str(start_line or "").strip() else 0
    end = _clamp_int(end_line, 0, 10**9, 0) if str(end_line or "").strip() else 0
    if start <= 0 and end <= 0:
        return []
    if start <= 0:
        start = 1
    if end > 0 and end < start:
        raise GitError(f"end_line({end}) 不能小于 start_line({start})")
    if end > 0:
        return ["-L", f"{start},{end}"]
    return ["-L", str(start)]


@mcp.tool(annotations=_READ_ANNOTATIONS)
def git_status(repo: str) -> dict:
    """查看仓库工作区状态(只读)。

    参数:
    - repo: 仓库路径, 必须是 GIT_MCP_ROOTS 白名单内的 git 仓库根目录(相对路径基于第一个 root)

    返回 ok/repo/output(porcelain=v1, 含分支跟踪信息)/truncated。
    未配置 GIT_MCP_ROOTS、路径越界、非 git 仓库或裸仓库时返回 {"ok": false, "error": "..."}。
    """
    logger.info(f"git_status: {repo}")
    try:
        path, is_bare = _resolve_repo(repo)
        _require_worktree(is_bare, "git_status")
        return _read_output(path, ["status", "--porcelain=v1", "--branch"])
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def git_log(repo: str, limit: int = 20, file: str = "") -> dict:
    """查看提交历史(只读, 裸仓库可用)。

    参数:
    - repo: GIT_MCP_ROOTS 内的 git 仓库根目录
    - limit: 最多返回的提交数(1-200), 默认 20
    - file: 仅看指定文件的历史(仓库内相对路径), 留空表示全部

    返回 ok/repo/limit/file/output(每条: 短哈希 日期 作者 装饰 主题)/truncated。
    """
    logger.info(f"git_log: {repo} limit={limit} file={file!r}")
    try:
        path, _ = _resolve_repo(repo)
        count = _clamp_int(limit, 1, MAX_LOG_LIMIT, 20)
        args = ["log", f"--max-count={count}", "--date=short", "--pretty=format:%h %ad %an %d %s"]
        path_arg = ""
        if str(file or "").strip():
            path_arg = _require_path(file, "file")
            args += ["--", path_arg]
        return _read_output(path, args, {"limit": count, "file": path_arg})
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def git_diff(repo: str, ref: str = "", staged: bool = False) -> dict:
    """查看工作区/暂存区差异(只读, 需要工作区)。

    参数:
    - repo: GIT_MCP_ROOTS 内的 git 仓库根目录
    - ref: 可选基线(分支/提交/标签), 留空表示工作区相对暂存区
    - staged: true 时显示已暂存差异(--cached)

    返回 ok/repo/ref/staged/output/truncated。裸仓库拒绝。
    """
    logger.info(f"git_diff: {repo} ref={ref!r} staged={staged}")
    try:
        path, is_bare = _resolve_repo(repo)
        _require_worktree(is_bare, "git_diff")
        args = ["diff", "--no-color"]
        if staged:
            args.append("--cached")
        target = ""
        if str(ref or "").strip():
            target = _require_ref(ref, "ref")
            args.append(target)
        return _read_output(path, args, {"ref": target, "staged": bool(staged)})
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def git_show(repo: str, ref: str = "HEAD") -> dict:
    """查看某次提交的详情与改动(只读, 裸仓库可用)。

    参数:
    - repo: GIT_MCP_ROOTS 内的 git 仓库根目录
    - ref: 分支/提交/标签, 默认 HEAD

    返回 ok/repo/ref/output(提交信息+补丁)/truncated。
    """
    logger.info(f"git_show: {repo} ref={ref!r}")
    try:
        path, _ = _resolve_repo(repo)
        target = _require_ref(ref or "HEAD", "ref")
        return _read_output(path, ["show", "--no-color", target], {"ref": target})
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def git_blame(repo: str, file: str, start_line: int = 0, end_line: int = 0) -> dict:
    """逐行查看文件 blame(只读, 需要工作区)。

    参数:
    - repo: GIT_MCP_ROOTS 内的 git 仓库根目录
    - file: 仓库内相对路径
    - start_line/end_line: 可选行范围(从 1 开始), 都为 0 表示整个文件

    返回 ok/repo/file/output/truncated; end_line 小于 start_line 时返回可读错误。
    """
    logger.info(f"git_blame: {repo} file={file!r} lines={start_line}-{end_line}")
    try:
        path, is_bare = _resolve_repo(repo)
        _require_worktree(is_bare, "git_blame")
        target = _require_path(file, "file")
        lines = _line_range(start_line, end_line)
        return _read_output(path, ["blame", *lines, "--", target], {"file": target})
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def git_branches(repo: str) -> dict:
    """列出本地分支(只读, 裸仓库可用)。

    返回 ok/repo/count/output(每行一个分支, '*' 为当前分支)/truncated。
    """
    logger.info(f"git_branches: {repo}")
    try:
        path, _ = _resolve_repo(repo)
        output = _run_checked(path, ["branch", "--no-color", "--list"])
        count = len([line for line in output.splitlines() if line.strip()])
        text, truncated = _truncate(output, _max_output_chars())
        return {"ok": True, "repo": str(path), "count": count, "output": text, "truncated": truncated}
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def git_add(repo: str, paths: list[str] | None = None, all: bool = False) -> dict:
    """把文件改动加入暂存区(写操作, 默认关闭; 需 GIT_MCP_ALLOW_WRITE=true)。

    参数:
    - repo: GIT_MCP_ROOTS 内的 git 仓库根目录
    - paths: 仓库内相对路径列表(不允许绝对路径或 '..'); 与 all 至少提供一个
    - all: true 时等价于 git add --all(暂存所有改动)

    返回 ok/repo/all/paths; 不会自动提交, 请另行调用 git_commit(保持显式两步)。
    """
    logger.info(f"git_add: {repo} paths={paths!r} all={all}")
    try:
        _require_write()
        path, is_bare = _resolve_repo(repo)
        _require_worktree(is_bare, "git_add")
        cleaned: list[str] = []
        if all:
            args = ["add", "--all"]
        else:
            cleaned = [_require_path(item, "paths") for item in (paths or []) if str(item or "").strip()]
            if not cleaned:
                raise GitError("paths 为空: 需提供至少一个仓库内相对路径, 或传 all=true")
            args = ["add", "--", *cleaned]
        _run_checked(path, args)
        return {"ok": True, "repo": str(path), "all": bool(all), "paths": cleaned}
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def git_commit(repo: str, message: str) -> dict:
    """提交已暂存改动(写操作, 默认关闭; 需 GIT_MCP_ALLOW_WRITE=true)。

    不会自动 git add: 请先调用 git_add。message 必填且不能为空白。
    返回 ok/repo/message/output(commit 摘要)/truncated。
    """
    logger.info(f"git_commit: {repo}")
    try:
        _require_write()
        path, is_bare = _resolve_repo(repo)
        _require_worktree(is_bare, "git_commit")
        text = str(message or "").strip()
        if not text:
            raise GitError("message 不能为空: 请提供提交说明(本工具不会自动 git add)")
        if "\x00" in text:
            raise GitError("message 不允许包含 NUL 字符")
        output = _run_checked(path, ["commit", "-m", text])
        summary, truncated = _truncate(output, _max_output_chars())
        return {"ok": True, "repo": str(path), "message": text, "output": summary, "truncated": truncated}
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def git_checkout(repo: str, ref: str, create: bool = False) -> dict:
    """切换分支/检出提交(写操作, 默认关闭; 需 GIT_MCP_ALLOW_WRITE=true)。

    参数:
    - repo: GIT_MCP_ROOTS 内的 git 仓库根目录
    - ref: 分支/提交/标签
    - create: true 时新建并切换到该分支(git checkout -b)

    返回 ok/repo/ref/created/output/truncated。裸仓库拒绝。
    """
    logger.info(f"git_checkout: {repo} ref={ref!r} create={create}")
    try:
        _require_write()
        path, is_bare = _resolve_repo(repo)
        _require_worktree(is_bare, "git_checkout")
        target = _require_ref(ref, "ref")
        args = ["checkout", "-b", target] if create else ["checkout", target]
        output = _run_checked(path, args)
        text, truncated = _truncate(output, _max_output_chars())
        return {
            "ok": True,
            "repo": str(path),
            "ref": target,
            "created": bool(create),
            "output": text,
            "truncated": truncated,
        }
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def git_branch_create(repo: str, name: str) -> dict:
    """新建分支(写操作, 默认关闭; 需 GIT_MCP_ALLOW_WRITE=true, 裸仓库可用)。

    返回 ok/repo/name/output/truncated。
    """
    logger.info(f"git_branch_create: {repo} name={name!r}")
    try:
        _require_write()
        path, _ = _resolve_repo(repo)
        branch = _require_ref(name, "name")
        output = _run_checked(path, ["branch", branch])
        text, truncated = _truncate(output, _max_output_chars())
        return {"ok": True, "repo": str(path), "name": branch, "output": text, "truncated": truncated}
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def git_branch_delete(repo: str, name: str, force: bool = False) -> dict:
    """删除分支(写操作, 默认关闭; 需 GIT_MCP_ALLOW_WRITE=true, 裸仓库可用)。

    参数:
    - force: true 时用 -D 强制删除未合并分支; 默认 -d(只删已合并分支)

    返回 ok/repo/name/force/output/truncated。
    """
    logger.info(f"git_branch_delete: {repo} name={name!r} force={force}")
    try:
        _require_write()
        path, _ = _resolve_repo(repo)
        branch = _require_ref(name, "name")
        output = _run_checked(path, ["branch", "-D" if force else "-d", branch])
        text, truncated = _truncate(output, _max_output_chars())
        return {
            "ok": True,
            "repo": str(path),
            "name": branch,
            "force": bool(force),
            "output": text,
            "truncated": truncated,
        }
    except GitError as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
