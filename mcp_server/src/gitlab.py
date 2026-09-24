"""GitLab MCP Server —— 内网 GitLab(默认 http://gitlab.xzrobot.com)读写工具。

鉴权优先级:
1. GITLAB_TOKEN: Personal Access Token, 请求带 PRIVATE-TOKEN 头;
2. GITLAB_USERNAME + GITLAB_PASSWORD(默认回退 IT_SYSTEM_PASSWORD): 走 LDAP 会话登录,
   流程与 config/agents/IT运维/skills/gitlab-merge/scripts/gitlab_merge.py 一致
   (GET /users/sign_in 取 authenticity_token → POST /users/auth/ldapmain/callback →
   GET / 取 csrf-token, 写请求带 X-CSRF-Token)。

环境变量: GITLAB_URL / GITLAB_TOKEN / GITLAB_USERNAME / GITLAB_PASSWORD /
GITLAB_MCP_TIMEOUT(默认30s) / GITLAB_MCP_MAX_OUTPUT_CHARS(默认40000)。
"""
import json
import logging
import os
import re
import threading
import urllib.parse
from typing import Any

import requests
from env_guard import require_secret
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

logger = logging.getLogger("mcp.gitlab")

mcp = MCPServer("Rosiwit MCP Server")

_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_SAFE_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

DEFAULT_BASE_URL = "http://gitlab.xzrobot.com"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_OUTPUT_CHARS = 40000
DEFAULT_MAX_DIFF_CHARS = 20000
MAX_PER_PAGE = 100


class GitLabError(Exception):
    """GitLab 调用失败, message 为可读中文原因。"""


# 同步 handler 在 MCP SDK v2 中运行于 anyio worker 线程; 同一 server 可能并发调用,
# 会话与登录态用 RLock 串行保护(内网低频工具, 串行开销可忽略)。
_lock = threading.RLock()
_session = requests.Session()
_csrf_token = ""
_logged_in = False

GITLAB_TOKEN = (os.getenv("GITLAB_TOKEN") or "").strip()
GITLAB_USERNAME = (os.getenv("GITLAB_USERNAME") or "s_software").strip()


def _password() -> str:
    """调用时解析口令（缺密钥时报清晰错误；服务仍可启动并列出工具）。"""
    if GITLAB_TOKEN:
        return ""
    return require_secret(
        "GITLAB_PASSWORD", os.getenv("GITLAB_PASSWORD") or os.getenv("IT_SYSTEM_PASSWORD")
    ) or ""


def _base_url() -> str:
    return (os.getenv("GITLAB_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL).rstrip("/")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _timeout() -> int:
    return _env_int("GITLAB_MCP_TIMEOUT", DEFAULT_TIMEOUT)


def _max_output_chars() -> int:
    return _env_int("GITLAB_MCP_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS)


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


def _tail_lines(text: str, lines: int) -> tuple[str, bool]:
    """取文本末尾 N 行, 返回 (文本, 是否被截断)。"""
    value = text or ""
    rows = value.splitlines()
    if len(rows) <= lines:
        return value, False
    return "\n".join(rows[-lines:]), True


def _encode_project(project: str) -> str:
    """项目标识: 数字 ID 原样; 命名空间路径做全量 URL 编码(GitLab 要求 %2F)。"""
    value = (project or "").strip()
    if not value:
        raise GitLabError("project 不能为空(支持数字 ID 或 group/subgroup/project 路径)")
    if value.isdigit():
        return value
    return urllib.parse.quote(value, safe="")


def _describe_error(resp: requests.Response) -> str:
    """把失败响应压缩成中文原因, 只取 message/error 字段, 不泄漏请求头。"""
    detail: Any = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = data.get("message") or data.get("error") or ""
    except ValueError:
        detail = (resp.text or "").strip()[:300]
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, ensure_ascii=False)
    detail = str(detail).strip()[:300]
    reason = f"GitLab API HTTP {resp.status_code}"
    return f"{reason}: {detail}" if detail else reason


def _ensure_login() -> None:
    """PAT 缺失时走 LDAP 会话登录(幂等, 由调用方持锁)。"""
    global _csrf_token, _logged_in
    if GITLAB_TOKEN or _logged_in:
        return
    password = _password()
    if not password:
        raise GitLabError(
            "未配置 GitLab 凭证: 请设置 GITLAB_TOKEN(推荐, Personal Access Token), "
            "或设置 GITLAB_PASSWORD(或 IT_SYSTEM_PASSWORD)以走 LDAP 会话登录"
        )
    base = _base_url()
    try:
        sign_in = _session.get(f"{base}/users/sign_in", timeout=_timeout())
        if sign_in.status_code != 200:
            raise GitLabError(f"GitLab 登录页异常: HTTP {sign_in.status_code}")
        match = re.search(r'name="authenticity_token" value="([^"]+)"', sign_in.text)
        if not match:
            raise GitLabError("GitLab 登录页缺少 authenticity_token, 站点结构可能已变化, 建议改用 GITLAB_TOKEN")
        login = _session.post(
            f"{base}/users/auth/ldapmain/callback",
            data={
                "authenticity_token": match.group(1),
                "username": GITLAB_USERNAME,
                "password": password,
            },
            timeout=_timeout(),
        )
        if login.status_code != 200:
            raise GitLabError(f"GitLab LDAP 登录失败: HTTP {login.status_code}(请检查账号/口令)")
        home = _session.get(f"{base}/", timeout=_timeout())
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', home.text)
        if not csrf:
            csrf = re.search(r'name="authenticity_token" value="([^"]+)"', home.text)
        _csrf_token = csrf.group(1) if csrf else ""
        check = _session.get(f"{base}/api/v4/user", timeout=_timeout())
        if check.status_code != 200:
            raise GitLabError(f"GitLab 会话校验失败: HTTP {check.status_code}")
        _logged_in = True
    except requests.RequestException as exc:
        raise GitLabError(f"GitLab 登录请求失败: {exc.__class__.__name__}") from None


def _request(method: str, path: str, *, params=None, payload=None, ok=(200,), text: bool = False):
    """统一请求入口(测试 mock 点): 锁内登录 + 发请求 + 状态码校验 + 解析。"""
    with _lock:
        _ensure_login()
        headers: dict[str, str] = {}
        if GITLAB_TOKEN:
            headers["PRIVATE-TOKEN"] = GITLAB_TOKEN
        if _csrf_token:
            headers["X-CSRF-Token"] = _csrf_token
        url = f"{_base_url()}/api/v4{path}"
        try:
            resp = _session.request(method, url, params=params, json=payload, headers=headers, timeout=_timeout())
        except requests.RequestException as exc:
            raise GitLabError(f"GitLab 请求失败: {exc.__class__.__name__}") from None
        if resp.status_code not in ok:
            raise GitLabError(_describe_error(resp))
        if text:
            return resp.text
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise GitLabError("GitLab 返回非 JSON 内容") from None


def _paged(items: list, page: int, per_page: int) -> dict:
    return {"items": items, "page": page, "per_page": per_page, "count": len(items), "has_more": len(items) >= per_page}


def _user_name(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("username") or user.get("name") or "")
    return ""


# ---------------------------------------------------------------- 读工具

@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_whoami() -> dict:
    """查看当前 GitLab 认证身份(用于校验 GITLAB_TOKEN 或 LDAP 会话是否可用)。"""
    try:
        user = _request("GET", "/user")
        return {
            "ok": True,
            "id": user.get("id"),
            "username": user.get("username"),
            "name": user.get("name"),
            "state": user.get("state"),
            "web_url": user.get("web_url"),
        }
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_list_projects(search: str = "", membership: bool = False, page: int = 1, per_page: int = 20) -> dict:
    """搜索/列出 GitLab 项目(按最近活跃排序)。search 支持名称或路径关键字。"""
    try:
        page = _clamp_int(page, 1, 10000, 1)
        per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 20)
        params: dict[str, Any] = {
            "simple": "true",
            "order_by": "last_activity_at",
            "page": page,
            "per_page": per_page,
        }
        if search.strip():
            params["search"] = search.strip()
        if membership:
            params["membership"] = "true"
        items = _request("GET", "/projects", params=params)
        projects = [
            {
                "id": item.get("id"),
                "path_with_namespace": item.get("path_with_namespace"),
                "name": item.get("name"),
                "default_branch": item.get("default_branch"),
                "visibility": item.get("visibility"),
                "last_activity_at": item.get("last_activity_at"),
                "web_url": item.get("web_url"),
            }
            for item in items
        ]
        return {"ok": True, **_paged(projects, page, per_page)}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_project(project: str) -> dict:
    """获取项目详情。project 支持数字 ID 或 group/subgroup/project 路径。"""
    try:
        data = _request("GET", f"/projects/{_encode_project(project)}")
        return {
            "ok": True,
            "id": data.get("id"),
            "name": data.get("name"),
            "path_with_namespace": data.get("path_with_namespace"),
            "description": _truncate(str(data.get("description") or ""), 2000)[0],
            "default_branch": data.get("default_branch"),
            "visibility": data.get("visibility"),
            "web_url": data.get("web_url"),
            "last_activity_at": data.get("last_activity_at"),
            "open_issues_count": data.get("open_issues_count"),
            "archived": data.get("archived"),
        }
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_list_branches(project: str, search: str = "", page: int = 1, per_page: int = 50) -> dict:
    """列出项目分支(含最新提交摘要)。"""
    try:
        page = _clamp_int(page, 1, 10000, 1)
        per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 50)
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if search.strip():
            params["search"] = search.strip()
        items = _request("GET", f"/projects/{_encode_project(project)}/repository/branches", params=params)
        branches = [
            {
                "name": item.get("name"),
                "default": item.get("default"),
                "protected": item.get("protected"),
                "merged": item.get("merged"),
                "commit": {
                    "id": (item.get("commit") or {}).get("id"),
                    "short_id": (item.get("commit") or {}).get("short_id"),
                    "title": (item.get("commit") or {}).get("title"),
                    "authored_date": (item.get("commit") or {}).get("authored_date"),
                },
            }
            for item in items
        ]
        return {"ok": True, **_paged(branches, page, per_page)}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_list_merge_requests(
    project: str = "",
    state: str = "opened",
    scope: str = "",
    source_branch: str = "",
    target_branch: str = "",
    author: str = "",
    search: str = "",
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """列出合并请求。project 留空 = 当前用户在所有项目的 MR(scope=created_by_me/assigned_to_me)。"""
    try:
        page = _clamp_int(page, 1, 10000, 1)
        per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 20)
        params: dict[str, Any] = {"state": state, "page": page, "per_page": per_page, "order_by": "updated_at"}
        if scope.strip():
            params["scope"] = scope.strip()
        if source_branch.strip():
            params["source_branch"] = source_branch.strip()
        if target_branch.strip():
            params["target_branch"] = target_branch.strip()
        if author.strip():
            params["author_username"] = author.strip()
        if search.strip():
            params["search"] = search.strip()
        path = f"/projects/{_encode_project(project)}/merge_requests" if project.strip() else "/merge_requests"
        items = _request("GET", path, params=params)
        mes = [
            {
                "iid": item.get("iid"),
                "title": item.get("title"),
                "state": item.get("state"),
                "draft": item.get("draft"),
                "source_branch": item.get("source_branch"),
                "target_branch": item.get("target_branch"),
                "author": _user_name(item.get("author")),
                "assignees": [_user_name(u) for u in (item.get("assignees") or [])],
                "labels": item.get("labels"),
                "merge_status": item.get("merge_status"),
                "updated_at": item.get("updated_at"),
                "web_url": item.get("web_url"),
            }
            for item in items
        ]
        return {"ok": True, **_paged(mes, page, per_page)}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_merge_request(project: str, mr_iid: int, include_approvals: bool = False) -> dict:
    """获取单个合并请求详情; include_approvals=True 时附带审批人列表。"""
    try:
        encoded = _encode_project(project)
        data = _request("GET", f"/projects/{encoded}/merge_requests/{int(mr_iid)}")
        result: dict[str, Any] = {
            "ok": True,
            "iid": data.get("iid"),
            "title": data.get("title"),
            "state": data.get("state"),
            "draft": data.get("draft"),
            "description": _truncate(str(data.get("description") or ""), 4000)[0],
            "source_branch": data.get("source_branch"),
            "target_branch": data.get("target_branch"),
            "author": _user_name(data.get("author")),
            "assignees": [_user_name(u) for u in (data.get("assignees") or [])],
            "reviewers": [_user_name(u) for u in (data.get("reviewers") or [])],
            "labels": data.get("labels"),
            "merge_status": data.get("merge_status"),
            "has_conflicts": data.get("has_conflicts"),
            "changes_count": data.get("changes_count"),
            "user_notes_count": data.get("user_notes_count"),
            "diff_refs": data.get("diff_refs"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "web_url": data.get("web_url"),
        }
        if include_approvals:
            approvals = _request("GET", f"/projects/{encoded}/merge_requests/{int(mr_iid)}/approvals")
            result["approved"] = approvals.get("approved")
            result["approved_by"] = [_user_name(u) for u in (approvals.get("approved_by") or [])]
        return result
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_mr_changes(
    project: str,
    mr_iid: int,
    file: str = "",
    include_diff: bool = True,
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> dict:
    """获取 MR 变更(两步评审: 先 include_diff=False 看文件清单, 再按 file 取单文件 diff)。

    单文件 diff 截断到 max_diff_chars; 总输出截断到 GITLAB_MCP_MAX_OUTPUT_CHARS。
    """
    try:
        encoded = _encode_project(project)
        data = _request("GET", f"/projects/{encoded}/merge_requests/{int(mr_iid)}/changes")
        changes = data.get("changes") or []
        max_diff_chars = _clamp_int(max_diff_chars, 500, _max_output_chars(), DEFAULT_MAX_DIFF_CHARS)
        if file.strip():
            changes = [c for c in changes if file.strip() in ((c.get("old_path") or ""), (c.get("new_path") or ""))]
            if not changes:
                raise GitLabError(f"MR !{int(mr_iid)} 中没有路径匹配 {file!r} 的变更文件")
        total_budget = _max_output_chars()
        used = 0
        overall_truncated = False
        files: list[dict[str, Any]] = []
        for change in changes:
            entry: dict[str, Any] = {
                "old_path": change.get("old_path"),
                "new_path": change.get("new_path"),
                "new_file": change.get("new_file"),
                "deleted_file": change.get("deleted_file"),
                "renamed_file": change.get("renamed_file"),
            }
            if include_diff:
                diff, truncated = _truncate(str(change.get("diff") or ""), max_diff_chars)
                if not overall_truncated and used + len(diff) > total_budget:
                    diff, truncated = "", True
                    overall_truncated = True
                used += len(diff)
                entry["diff"] = diff
                entry["diff_truncated"] = truncated
            files.append(entry)
        return {
            "ok": True,
            "iid": int(mr_iid),
            "file_count": len(files),
            "files": files,
            "include_diff": include_diff,
            "overall_truncated": overall_truncated,
        }
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_list_mr_notes(project: str, mr_iid: int, sort: str = "asc", page: int = 1, per_page: int = 50) -> dict:
    """列出 MR 评论/系统记录(旧→新)。"""
    try:
        page = _clamp_int(page, 1, 10000, 1)
        per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 50)
        items = _request(
            "GET",
            f"/projects/{_encode_project(project)}/merge_requests/{int(mr_iid)}/notes",
            params={"sort": sort, "page": page, "per_page": per_page},
        )
        notes = [
            {
                "id": item.get("id"),
                "author": _user_name(item.get("author")),
                "created_at": item.get("created_at"),
                "system": item.get("system"),
                "body": _truncate(str(item.get("body") or ""), 2000)[0],
            }
            for item in items
        ]
        return {"ok": True, **_paged(notes, page, per_page)}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_list_issues(
    project: str = "",
    state: str = "opened",
    labels: str = "",
    assignee: str = "",
    search: str = "",
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """列出 issue。project 留空 = 当前用户在所有项目的 issue。labels 逗号分隔。"""
    try:
        page = _clamp_int(page, 1, 10000, 1)
        per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 20)
        params: dict[str, Any] = {"state": state, "page": page, "per_page": per_page}
        if labels.strip():
            params["labels"] = labels.strip()
        if assignee.strip():
            params["assignee_username"] = assignee.strip()
        if search.strip():
            params["search"] = search.strip()
        path = f"/projects/{_encode_project(project)}/issues" if project.strip() else "/issues"
        items = _request("GET", path, params=params)
        issues = [
            {
                "iid": item.get("iid"),
                "title": item.get("title"),
                "state": item.get("state"),
                "author": _user_name(item.get("author")),
                "assignees": [_user_name(u) for u in (item.get("assignees") or [])],
                "labels": item.get("labels"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "web_url": item.get("web_url"),
            }
            for item in items
        ]
        return {"ok": True, **_paged(issues, page, per_page)}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_issue(project: str, issue_iid: int) -> dict:
    """获取单个 issue 详情(描述截断)。"""
    try:
        data = _request("GET", f"/projects/{_encode_project(project)}/issues/{int(issue_iid)}")
        description, truncated = _truncate(str(data.get("description") or ""), 4000)
        return {
            "ok": True,
            "iid": data.get("iid"),
            "title": data.get("title"),
            "state": data.get("state"),
            "description": description,
            "description_truncated": truncated,
            "author": _user_name(data.get("author")),
            "assignees": [_user_name(u) for u in (data.get("assignees") or [])],
            "labels": data.get("labels"),
            "milestone": (data.get("milestone") or {}).get("title"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "web_url": data.get("web_url"),
        }
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_list_commits(
    project: str,
    ref_name: str = "",
    path: str = "",
    since: str = "",
    until: str = "",
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """列出提交历史(since/until 为 ISO8601 时间, 可选)。"""
    try:
        page = _clamp_int(page, 1, 10000, 1)
        per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 20)
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if ref_name.strip():
            params["ref_name"] = ref_name.strip()
        if path.strip():
            params["path"] = path.strip()
        if since.strip():
            params["since"] = since.strip()
        if until.strip():
            params["until"] = until.strip()
        items = _request("GET", f"/projects/{_encode_project(project)}/repository/commits", params=params)
        commits = [
            {
                "id": item.get("id"),
                "short_id": item.get("short_id"),
                "title": item.get("title"),
                "author_name": item.get("author_name"),
                "authored_date": item.get("authored_date"),
                "committed_date": item.get("committed_date"),
                "web_url": item.get("web_url"),
            }
            for item in items
        ]
        return {"ok": True, **_paged(commits, page, per_page)}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_commit_diff(project: str, sha: str, max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS) -> dict:
    """获取某个提交的变更文件与 diff(单文件截断)。"""
    try:
        max_diff_chars = _clamp_int(max_diff_chars, 500, _max_output_chars(), DEFAULT_MAX_DIFF_CHARS)
        items = _request("GET", f"/projects/{_encode_project(project)}/repository/commits/{sha.strip()}/diff")
        files = []
        for change in items:
            diff, truncated = _truncate(str(change.get("diff") or ""), max_diff_chars)
            files.append(
                {
                    "old_path": change.get("old_path"),
                    "new_path": change.get("new_path"),
                    "new_file": change.get("new_file"),
                    "deleted_file": change.get("deleted_file"),
                    "renamed_file": change.get("renamed_file"),
                    "diff": diff,
                    "diff_truncated": truncated,
                }
            )
        return {"ok": True, "sha": sha.strip(), "file_count": len(files), "files": files}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_file(project: str, file_path: str = "", ref: str = "", page: int = 1, per_page: int = 50) -> dict:
    """file_path 为空 = 列出目录(tree); 否则读取文件原文(raw, 超长截断)。"""
    try:
        encoded = _encode_project(project)
        if not file_path.strip():
            page = _clamp_int(page, 1, 10000, 1)
            per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 50)
            params = {"page": page, "per_page": per_page}
            if ref.strip():
                params["ref"] = ref.strip()
            items = _request("GET", f"/projects/{encoded}/repository/tree", params=params)
            entries = [
                {"name": item.get("name"), "path": item.get("path"), "type": item.get("type"), "mode": item.get("mode")}
                for item in items
            ]
            return {"ok": True, **_paged(entries, page, per_page)}
        params = {"ref": ref.strip()} if ref.strip() else {}
        raw = _request(
            "GET",
            f"/projects/{encoded}/repository/files/{urllib.parse.quote(file_path.strip(), safe='')}/raw",
            params=params,
            text=True,
        )
        content, truncated = _truncate(raw, _max_output_chars())
        return {
            "ok": True,
            "path": file_path.strip(),
            "ref": ref.strip() or "HEAD",
            "content": content,
            "truncated": truncated,
        }
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_list_pipelines(project: str, ref: str = "", status: str = "", page: int = 1, per_page: int = 20) -> dict:
    """列出流水线(ref/status 可选过滤)。"""
    try:
        page = _clamp_int(page, 1, 10000, 1)
        per_page = _clamp_int(per_page, 1, MAX_PER_PAGE, 20)
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if ref.strip():
            params["ref"] = ref.strip()
        if status.strip():
            params["status"] = status.strip()
        items = _request("GET", f"/projects/{_encode_project(project)}/pipelines", params=params)
        pipelines = [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "ref": item.get("ref"),
                "sha": item.get("sha"),
                "source": item.get("source"),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "web_url": item.get("web_url"),
            }
            for item in items
        ]
        return {"ok": True, **_paged(pipelines, page, per_page)}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_pipeline(project: str, pipeline_id: int) -> dict:
    """获取流水线详情(状态/耗时)及其作业列表(作业属于流水线, 归属标注于每条作业)。"""
    try:
        encoded = _encode_project(project)
        data = _request("GET", f"/projects/{encoded}/pipelines/{int(pipeline_id)}")
        jobs = _request(
            "GET", f"/projects/{encoded}/pipelines/{int(pipeline_id)}/jobs", params={"per_page": MAX_PER_PAGE}
        )
        return {
            "ok": True,
            "id": data.get("id"),
            "status": data.get("status"),
            "ref": data.get("ref"),
            "sha": data.get("sha"),
            "source": data.get("source"),
            "duration": data.get("duration"),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "web_url": data.get("web_url"),
            "jobs": [
                {
                    "id": job.get("id"),
                    "name": job.get("name"),
                    "stage": job.get("stage"),
                    "status": job.get("status"),
                    "duration": job.get("duration"),
                    "allow_failure": job.get("allow_failure"),
                    "web_url": job.get("web_url"),
                }
                for job in jobs
            ],
        }
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gitlab_get_job_log(project: str, job_id: int, lines: int = 200) -> dict:
    """获取作业(构建)日志末尾 lines 行(默认 200, 上限 2000)。"""
    try:
        lines = _clamp_int(lines, 1, 2000, 200)
        raw = _request("GET", f"/projects/{_encode_project(project)}/jobs/{int(job_id)}/trace", text=True)
        tail, truncated = _tail_lines(raw, lines)
        return {"ok": True, "job_id": int(job_id), "lines": lines, "log": tail, "truncated": truncated}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- 写工具

@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_create_merge_request(
    project: str,
    source_branch: str,
    target_branch: str,
    title: str = "",
    description: str = "",
    remove_source_branch: bool = False,
    squash: bool = False,
    assignee_id: int | None = None,
    reviewer_ids: list[int] | None = None,
) -> dict:
    """创建合并请求(title 留空自动生成)。"""
    try:
        payload: dict[str, Any] = {
            "source_branch": source_branch.strip(),
            "target_branch": target_branch.strip(),
            "title": title.strip() or f"Merge branch {source_branch.strip()} into {target_branch.strip()}",
            "description": description,
            "remove_source_branch": bool(remove_source_branch),
            "squash": bool(squash),
        }
        if assignee_id is not None:
            payload["assignee_id"] = int(assignee_id)
        if reviewer_ids:
            payload["reviewer_ids"] = [int(uid) for uid in reviewer_ids]
        data = _request("POST", f"/projects/{_encode_project(project)}/merge_requests", payload=payload, ok=(200, 201))
        logger.info(f"创建 MR !{data.get('iid')} ({source_branch} → {target_branch})")
        return {"ok": True, "iid": data.get("iid"), "state": data.get("state"), "web_url": data.get("web_url")}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_update_merge_request(
    project: str,
    mr_iid: int,
    title: str = "",
    description: str = "",
    add_labels: str = "",
    remove_labels: str = "",
    target_branch: str = "",
    state_event: str = "",
    assignee_id: int | None = None,
    reviewer_ids: list[int] | None = None,
) -> dict:
    """更新 MR(标题/描述/标签/目标分支/关闭重开)。state_event 取 close 或 reopen。"""
    try:
        payload: dict[str, Any] = {}
        if title.strip():
            payload["title"] = title.strip()
        if description:
            payload["description"] = description
        if add_labels.strip():
            payload["add_labels"] = add_labels.strip()
        if remove_labels.strip():
            payload["remove_labels"] = remove_labels.strip()
        if target_branch.strip():
            payload["target_branch"] = target_branch.strip()
        if state_event.strip():
            if state_event.strip() not in ("close", "reopen"):
                raise GitLabError("state_event 仅支持 close 或 reopen")
            payload["state_event"] = state_event.strip()
        if assignee_id is not None:
            payload["assignee_id"] = int(assignee_id)
        if reviewer_ids:
            payload["reviewer_ids"] = [int(uid) for uid in reviewer_ids]
        if not payload:
            raise GitLabError("至少提供一个要修改的字段(title/description/add_labels/remove_labels/target_branch/state_event/assignee_id/reviewer_ids)")
        data = _request(
            "PUT", f"/projects/{_encode_project(project)}/merge_requests/{int(mr_iid)}", payload=payload
        )
        logger.info(f"更新 MR !{int(mr_iid)}: {', '.join(payload)}")
        return {"ok": True, "iid": data.get("iid"), "state": data.get("state"), "web_url": data.get("web_url")}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_comment_mr(project: str, mr_iid: int, body: str, file: str = "", line: int = 0) -> dict:
    """评论 MR: 只给 body = 普通评论; 同时给 file+line = 指定行的行内评论(discussion)。"""
    try:
        encoded = _encode_project(project)
        iid = int(mr_iid)
        if not body.strip():
            raise GitLabError("body 不能为空")
        if file.strip() and int(line) > 0:
            mr = _request("GET", f"/projects/{encoded}/merge_requests/{iid}")
            refs = mr.get("diff_refs") or {}
            if not refs.get("base_sha"):
                raise GitLabError("MR 缺少 diff_refs, 无法创建行内评论")
            payload: dict[str, Any] = {
                "body": body,
                "position": {
                    "base_sha": refs.get("base_sha"),
                    "head_sha": refs.get("head_sha"),
                    "start_sha": refs.get("start_sha"),
                    "position_type": "text",
                    "new_path": file.strip(),
                    "new_line": int(line),
                },
            }
            data = _request("POST", f"/projects/{encoded}/merge_requests/{iid}/discussions", payload=payload, ok=(200, 201))
            logger.info(f"MR !{iid} 行内评论 {file}:{int(line)}")
            return {"ok": True, "discussion_id": data.get("id"), "inline": True}
        data = _request("POST", f"/projects/{encoded}/merge_requests/{iid}/notes", payload={"body": body}, ok=(200, 201))
        logger.info(f"MR !{iid} 评论")
        return {"ok": True, "note_id": data.get("id"), "inline": False}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_approve_mr(project: str, mr_iid: int, approved: bool = True) -> dict:
    """审批/取消审批 MR(approved=False 为 unapprove)。"""
    try:
        encoded = _encode_project(project)
        iid = int(mr_iid)
        action = "approve" if approved else "unapprove"
        data = _request("POST", f"/projects/{encoded}/merge_requests/{iid}/{action}", ok=(200, 201))
        logger.info(f"MR !{iid} {action}")
        return {"ok": True, "approved": bool(approved), "approval_count": len(data.get("approved_by") or [])}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def gitlab_merge_mr(
    project: str,
    mr_iid: int,
    squash: bool = False,
    merge_when_pipeline_succeeds: bool = False,
    remove_source_branch: bool = False,
) -> dict:
    """合并 MR(危险: 直接写入目标分支)。"""
    try:
        payload = {
            "squash": bool(squash),
            "merge_when_pipeline_succeeds": bool(merge_when_pipeline_succeeds),
            "should_remove_source_branch": bool(remove_source_branch),
        }
        data = _request(
            "PUT", f"/projects/{_encode_project(project)}/merge_requests/{int(mr_iid)}/merge", payload=payload
        )
        state = data.get("state")
        if state != "merged" and data.get("message"):
            raise GitLabError(f"合并未完成: {data.get('message')}")
        logger.info(f"合并 MR !{int(mr_iid)}: state={state}")
        return {
            "ok": state == "merged",
            "state": state,
            "merge_commit_sha": data.get("merge_commit_sha"),
            "merged_at": data.get("merged_at"),
        }
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_create_issue(
    project: str, title: str, description: str = "", labels: str = "", assignee_id: int | None = None
) -> dict:
    """创建 issue(labels 逗号分隔)。"""
    try:
        if not title.strip():
            raise GitLabError("title 不能为空")
        payload: dict[str, Any] = {"title": title.strip(), "description": description}
        if labels.strip():
            payload["labels"] = labels.strip()
        if assignee_id is not None:
            payload["assignee_id"] = int(assignee_id)
        data = _request("POST", f"/projects/{_encode_project(project)}/issues", payload=payload, ok=(200, 201))
        logger.info(f"创建 issue #{data.get('iid')}")
        return {"ok": True, "iid": data.get("iid"), "state": data.get("state"), "web_url": data.get("web_url")}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_update_issue(
    project: str,
    issue_iid: int,
    title: str = "",
    description: str = "",
    add_labels: str = "",
    remove_labels: str = "",
    state_event: str = "",
    assignee_id: int | None = None,
) -> dict:
    """更新 issue(标题/描述/标签/关闭重开)。state_event 取 close 或 reopen。"""
    try:
        payload: dict[str, Any] = {}
        if title.strip():
            payload["title"] = title.strip()
        if description:
            payload["description"] = description
        if add_labels.strip():
            payload["add_labels"] = add_labels.strip()
        if remove_labels.strip():
            payload["remove_labels"] = remove_labels.strip()
        if state_event.strip():
            if state_event.strip() not in ("close", "reopen"):
                raise GitLabError("state_event 仅支持 close 或 reopen")
            payload["state_event"] = state_event.strip()
        if assignee_id is not None:
            payload["assignee_id"] = int(assignee_id)
        if not payload:
            raise GitLabError("至少提供一个要修改的字段")
        data = _request("PUT", f"/projects/{_encode_project(project)}/issues/{int(issue_iid)}", payload=payload)
        logger.info(f"更新 issue #{int(issue_iid)}: {', '.join(payload)}")
        return {"ok": True, "iid": data.get("iid"), "state": data.get("state"), "web_url": data.get("web_url")}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_run_pipeline(project: str, ref: str, variables: dict | None = None) -> dict:
    """在指定分支/标签上触发流水线(variables 为 key→value 字典)。"""
    try:
        if not ref.strip():
            raise GitLabError("ref 不能为空")
        payload: dict[str, Any] = {"ref": ref.strip()}
        if variables:
            payload["variables"] = [{"key": str(k), "value": str(v)} for k, v in variables.items()]
        data = _request("POST", f"/projects/{_encode_project(project)}/pipeline", payload=payload, ok=(200, 201))
        logger.info(f"触发流水线 {data.get('id')} on {ref.strip()}")
        return {"ok": True, "id": data.get("id"), "status": data.get("status"), "web_url": data.get("web_url")}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gitlab_retry_pipeline(project: str, pipeline_id: int) -> dict:
    """重试流水线中失败的作业。"""
    try:
        data = _request("POST", f"/projects/{_encode_project(project)}/pipelines/{int(pipeline_id)}/retry", ok=(200, 201))
        logger.info(f"重试流水线 {int(pipeline_id)}")
        return {"ok": True, "id": data.get("id"), "status": data.get("status"), "web_url": data.get("web_url")}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def gitlab_cancel_pipeline(project: str, pipeline_id: int) -> dict:
    """取消流水线(危险: 中断正在进行的构建)。"""
    try:
        data = _request("POST", f"/projects/{_encode_project(project)}/pipelines/{int(pipeline_id)}/cancel", ok=(200, 201))
        logger.info(f"取消流水线 {int(pipeline_id)}")
        return {"ok": True, "id": data.get("id"), "status": data.get("status"), "web_url": data.get("web_url")}
    except GitLabError as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
