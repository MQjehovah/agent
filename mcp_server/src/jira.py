"""Jira MCP Server —— 内网 Jira Server/Data Center(默认 http://jira.xzrobot.com)读写工具。

鉴权:
1. JIRA_TOKEN: Data Center Personal Access Token, 请求带 Authorization: Bearer;
2. JIRA_USERNAME + JIRA_PASSWORD(默认回退 IT_SYSTEM_PASSWORD): HTTP Basic。

使用 Jira Server/DC 的 REST API v2(描述为纯文本/wiki markup); 适配 Jira 8.14+。
环境变量: JIRA_URL / JIRA_TOKEN / JIRA_USERNAME / JIRA_PASSWORD /
JIRA_MCP_TIMEOUT(默认30s) / JIRA_MCP_MAX_OUTPUT_CHARS(默认40000)。
"""
import logging
import os
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

logger = logging.getLogger("mcp.jira")

mcp = MCPServer("Rosiwit MCP Server")

_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_SAFE_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

DEFAULT_BASE_URL = "http://jira.xzrobot.com"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_OUTPUT_CHARS = 40000
MAX_RESULTS_LIMIT = 100
DEFAULT_SEARCH_FIELDS = (
    "summary,status,assignee,reporter,priority,issuetype,labels,created,updated,resolution,project"
)
DETAIL_FIELDS = (
    "summary,status,assignee,reporter,priority,issuetype,labels,created,updated,resolution,project,"
    "description,components,duedate,fixVersions,parent,issuelinks,subtasks,comment"
)


class JiraError(Exception):
    """Jira 调用失败, message 为可读中文原因。"""


# 同步 handler 在 MCP SDK v2 中运行于 anyio worker 线程; 会话用 RLock 串行保护。
_lock = threading.RLock()
_session = requests.Session()

JIRA_TOKEN = (os.getenv("JIRA_TOKEN") or "").strip()
JIRA_USERNAME = (os.getenv("JIRA_USERNAME") or "s_software").strip()
if JIRA_TOKEN:
    JIRA_PASSWORD = ""
else:
    JIRA_PASSWORD = require_secret(
        "JIRA_PASSWORD", os.getenv("JIRA_PASSWORD") or os.getenv("IT_SYSTEM_PASSWORD")
    ) or ""


def _base_url() -> str:
    return (os.getenv("JIRA_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL).rstrip("/")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _timeout() -> int:
    return _env_int("JIRA_MCP_TIMEOUT", DEFAULT_TIMEOUT)


def _max_output_chars() -> int:
    return _env_int("JIRA_MCP_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS)


def _clamp_int(value, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(number, maximum))


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    value = text or ""
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _describe_error(resp: requests.Response) -> str:
    """压缩 Jira 错误(优先 errorMessages/errors), 不泄漏请求头。"""
    detail: Any = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            messages = data.get("errorMessages") or []
            errors = data.get("errors") or {}
            parts = [str(m) for m in messages] + [f"{k}: {v}" for k, v in errors.items()]
            detail = "; ".join(parts)
    except ValueError:
        detail = (resp.text or "").strip()[:300]
    detail = str(detail).strip()[:300]
    reason = f"Jira API HTTP {resp.status_code}"
    return f"{reason}: {detail}" if detail else reason


def _request(method: str, path: str, *, params=None, payload=None, ok=(200,), text: bool = False):
    """统一请求入口(测试 mock 点): Bearer/Basic 鉴权 + 状态码校验 + JSON 解析。"""
    with _lock:
        headers = {"Accept": "application/json"}
        auth = None
        if JIRA_TOKEN:
            headers["Authorization"] = f"Bearer {JIRA_TOKEN}"
        else:
            auth = (JIRA_USERNAME, JIRA_PASSWORD)
        url = f"{_base_url()}/rest/api/2{path}"
        try:
            resp = _session.request(
                method, url, params=params, json=payload, headers=headers, auth=auth, timeout=_timeout()
            )
        except requests.RequestException as exc:
            raise JiraError(f"Jira 请求失败: {exc.__class__.__name__}") from None
        if resp.status_code not in ok:
            raise JiraError(_describe_error(resp))
        if text:
            return resp.text
        if not resp.content or resp.status_code == 204:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise JiraError("Jira 返回非 JSON 内容") from None


def _user_summary(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("displayName") or user.get("name") or user.get("key") or "")
    return ""


def _seconds_to_text(seconds: Any) -> str:
    return f"{seconds}s" if isinstance(seconds, (int, float)) else ""


def _issue_slim(item: dict) -> dict:
    fields = item.get("fields") or {}
    return {
        "key": item.get("key"),
        "id": item.get("id"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "issuetype": (fields.get("issuetype") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "assignee": _user_summary(fields.get("assignee")),
        "reporter": _user_summary(fields.get("reporter")),
        "labels": fields.get("labels"),
        "resolution": (fields.get("resolution") or {}).get("name") if fields.get("resolution") else None,
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "project": (fields.get("project") or {}).get("key"),
    }


def _issue_detail(item: dict, include_comments: bool) -> dict:
    fields = item.get("fields") or {}
    description, desc_truncated = _truncate(str(fields.get("description") or ""), 4000)
    result = _issue_slim(item)
    result.update(
        {
            "ok": True,
            "description": description,
            "description_truncated": desc_truncated,
            "components": [c.get("name") for c in (fields.get("components") or [])],
            "fix_versions": [v.get("name") for v in (fields.get("fixVersions") or [])],
            "due_date": fields.get("duedate"),
            "parent": (fields.get("parent") or {}).get("key") if fields.get("parent") else None,
            "subtasks": [
                {"key": s.get("key"), "summary": (s.get("fields") or {}).get("summary"),
                 "status": ((s.get("fields") or {}).get("status") or {}).get("name")}
                for s in (fields.get("subtasks") or [])
            ],
            "issue_links": [
                {"type": (link.get("type") or {}).get("name"), "key": (link.get("outwardIssue") or link.get("inwardIssue") or {}).get("key")}
                for link in (fields.get("issuelinks") or [])
            ],
            "time_original_estimate": _seconds_to_text(fields.get("timeoriginalestimate")),
            "time_estimate": _seconds_to_text(fields.get("timeestimate")),
            "web_url": f"{_base_url()}/browse/{item.get('key')}",
        }
    )
    if include_comments:
        comments = []
        for comment in ((fields.get("comment") or {}).get("comments") or []):
            body, _ = _truncate(str(comment.get("body") or ""), 2000)
            comments.append(
                {
                    "id": comment.get("id"),
                    "author": _user_summary(comment.get("author")),
                    "created": comment.get("created"),
                    "body": body,
                }
            )
        result["comments"] = comments
    return result


# ---------------------------------------------------------------- 读工具

@mcp.tool(annotations=_READ_ANNOTATIONS)
def jira_server_info() -> dict:
    """查看 Jira 版本与当前认证身份(用于校验 JIRA_TOKEN 或账号口令)。"""
    try:
        info = _request("GET", "/serverInfo")
        myself = _request("GET", "/myself")
        return {
            "ok": True,
            "server_title": info.get("serverTitle"),
            "version": info.get("version"),
            "deployment_type": info.get("deploymentType"),
            "user": {
                "name": myself.get("name"),
                "key": myself.get("key"),
                "display_name": myself.get("displayName"),
                "email": myself.get("emailAddress"),
            },
        }
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def jira_search(jql: str, start_at: int = 0, max_results: int = 50, fields: str = "") -> dict:
    """JQL 搜索 issue(如 project = PROJ AND status = "In Progress" ORDER BY updated DESC)。"""
    try:
        if not jql.strip():
            raise JiraError("jql 不能为空")
        start_at = _clamp_int(start_at, 0, 1000000, 0)
        max_results = _clamp_int(max_results, 1, MAX_RESULTS_LIMIT, 50)
        params: dict[str, Any] = {
            "jql": jql.strip(),
            "startAt": start_at,
            "maxResults": max_results,
            "fields": fields.strip() or DEFAULT_SEARCH_FIELDS,
        }
        data = _request("GET", "/search", params=params)
        issues = [_issue_slim(item) for item in (data.get("issues") or [])]
        total = int(data.get("total") or 0)
        return {
            "ok": True,
            "issues": issues,
            "count": len(issues),
            "total": total,
            "start_at": start_at,
            "max_results": max_results,
            "has_more": start_at + len(issues) < total,
        }
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def jira_get_issue(issue_key: str, include_comments: bool = False) -> dict:
    """获取 issue 详情(描述/指派人/组件/子任务/链接; include_comments=True 附评论)。"""
    try:
        key = urllib.parse.quote((issue_key or "").strip(), safe="")
        if not key:
            raise JiraError("issue_key 不能为空(如 PROJ-123)")
        data = _request("GET", f"/issue/{key}", params={"fields": DETAIL_FIELDS})
        return _issue_detail(data, include_comments)
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def jira_list_projects() -> dict:
    """列出所有可见项目。"""
    try:
        items = _request("GET", "/project")
        projects = [
            {
                "key": item.get("key"),
                "id": item.get("id"),
                "name": item.get("name"),
                "project_type": item.get("projectTypeKey"),
                "lead": _user_summary(item.get("lead")),
                "archived": item.get("archived"),
            }
            for item in items
        ]
        return {"ok": True, "projects": projects, "count": len(projects)}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def jira_get_create_meta(project_key: str) -> dict:
    """获取项目可创建的 issue 类型及其必填/常用字段(建单前先查)。"""
    try:
        key = (project_key or "").strip()
        if not key:
            raise JiraError("project_key 不能为空")
        data = _request(
            "GET", "/issue/createmeta", params={"projectKeys": key, "expand": "projects.issuetypes.fields"}
        )
        projects = data.get("projects") or []
        if not projects:
            # 兼容已关闭 createmeta 的 Jira(8.4+): 退化为仅列 issue 类型
            types = _request("GET", f"/issue/createmeta/{urllib.parse.quote(key, safe='')}/issuetypes")
            return {
                "ok": True,
                "project": key,
                "issue_types": [
                    {"id": t.get("id"), "name": t.get("name"), "subtask": t.get("subtask")}
                    for t in (types or [])
                ],
                "fields_note": "该 Jira 未返回字段元数据, 请直接调用 jira_create_issue",
            }
        project = projects[0]
        issue_types = []
        for itype in (project.get("issuetypes") or []):
            fields = []
            for name, meta in (itype.get("fields") or {}).items():
                if not meta.get("required"):
                    continue
                allowed = [
                    v.get("name") or v.get("value")
                    for v in (meta.get("allowedValues") or [])[:20]
                ]
                fields.append({"name": name, "label": meta.get("name"), "allowed_values": allowed})
            issue_types.append(
                {"id": itype.get("id"), "name": itype.get("name"), "subtask": itype.get("subtask"), "required_fields": fields}
            )
        return {"ok": True, "project": project.get("key") or key, "issue_types": issue_types}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def jira_list_transitions(issue_key: str) -> dict:
    """列出 issue 当前可执行的流转(状态切换), 返回 id/name/目标状态。"""
    try:
        key = urllib.parse.quote((issue_key or "").strip(), safe="")
        if not key:
            raise JiraError("issue_key 不能为空")
        data = _request("GET", f"/issue/{key}/transitions", params={"expand": "transitions.fields"})
        transitions = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "to": (t.get("to") or {}).get("name"),
                "has_screen": bool(t.get("fields")),
            }
            for t in (data.get("transitions") or [])
        ]
        return {"ok": True, "transitions": transitions, "count": len(transitions)}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def jira_search_users(query: str, max_results: int = 20) -> dict:
    """按用户名/全名/邮箱搜索用户(指派前解析账号)。"""
    try:
        if not query.strip():
            raise JiraError("query 不能为空")
        max_results = _clamp_int(max_results, 1, 50, 20)
        try:
            items = _request("GET", "/user/search", params={"username": query.strip(), "maxResults": max_results})
        except JiraError as exc:
            if "404" not in str(exc):
                raise
            items = _request("GET", "/user/picker", params={"query": query.strip(), "maxResults": max_results})
            items = (items or {}).get("users") or []
        users = [
            {
                "name": item.get("name"),
                "key": item.get("key"),
                "display_name": item.get("displayName"),
                "email": item.get("emailAddress"),
                "active": item.get("active"),
            }
            for item in (items or [])
        ]
        return {"ok": True, "users": users, "count": len(users)}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- 写工具

@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def jira_create_issue(
    project: str,
    summary: str,
    issue_type: str = "Task",
    description: str = "",
    assignee: str = "",
    priority: str = "",
    labels: list[str] | None = None,
    components: list[str] | None = None,
    due_date: str = "",
) -> dict:
    """创建 issue(project/issue_type 可用名称或 ID; assignee 为账号 name)。"""
    try:
        if not project.strip() or not summary.strip():
            raise JiraError("project 与 summary 不能为空")
        fields: dict[str, Any] = {
            "project": {"key": project.strip()},
            "issuetype": {"name": issue_type.strip() or "Task"},
            "summary": summary.strip(),
        }
        if description:
            fields["description"] = description
        if assignee.strip():
            fields["assignee"] = {"name": assignee.strip()}
        if priority.strip():
            fields["priority"] = {"name": priority.strip()}
        if labels:
            fields["labels"] = [str(x) for x in labels]
        if components:
            fields["components"] = [{"name": str(x)} for x in components]
        if due_date.strip():
            fields["duedate"] = due_date.strip()
        data = _request("POST", "/issue", payload={"fields": fields}, ok=(200, 201))
        logger.info(f"创建 issue {data.get('key')}")
        return {"ok": True, "key": data.get("key"), "id": data.get("id"), "web_url": f"{_base_url()}/browse/{data.get('key')}"}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def jira_update_issue(
    issue_key: str,
    summary: str = "",
    description: str = "",
    assignee: str = "",
    priority: str = "",
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    due_date: str = "",
) -> dict:
    """更新 issue(仅传要改的字段); 标签增删走 add_labels/remove_labels。"""
    try:
        key = urllib.parse.quote((issue_key or "").strip(), safe="")
        if not key:
            raise JiraError("issue_key 不能为空")
        fields: dict[str, Any] = {}
        if summary.strip():
            fields["summary"] = summary.strip()
        if description:
            fields["description"] = description
        if assignee.strip():
            fields["assignee"] = {"name": assignee.strip()}
        if priority.strip():
            fields["priority"] = {"name": priority.strip()}
        if due_date.strip():
            fields["duedate"] = due_date.strip()
        update: dict[str, Any] = {}
        label_ops = [{"add": str(x)} for x in (add_labels or [])] + [{"remove": str(x)} for x in (remove_labels or [])]
        if label_ops:
            update["labels"] = label_ops
        if not fields and not update:
            raise JiraError("至少提供一个要修改的字段")
        payload: dict[str, Any] = {}
        if fields:
            payload["fields"] = fields
        if update:
            payload["update"] = update
        _request("PUT", f"/issue/{key}", payload=payload, ok=(200, 204))
        logger.info(f"更新 issue {issue_key.strip()}: {', '.join(list(fields) + list(update))}")
        return {"ok": True, "key": issue_key.strip(), "updated": list(fields) + list(update)}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def jira_add_comment(issue_key: str, body: str) -> dict:
    """给 issue 添加评论。"""
    try:
        if not body.strip():
            raise JiraError("body 不能为空")
        key = urllib.parse.quote((issue_key or "").strip(), safe="")
        if not key:
            raise JiraError("issue_key 不能为空")
        data = _request("POST", f"/issue/{key}/comment", payload={"body": body}, ok=(200, 201))
        logger.info(f"issue {issue_key.strip()} 添加评论")
        return {"ok": True, "id": data.get("id"), "created": data.get("created")}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def jira_transition_issue(issue_key: str, transition: str, resolution: str = "", comment: str = "") -> dict:
    """流转 issue(transition 传名称或 ID; 可附 resolution 与评论)。"""
    try:
        key = urllib.parse.quote((issue_key or "").strip(), safe="")
        if not key:
            raise JiraError("issue_key 不能为空")
        if not transition.strip():
            raise JiraError("transition 不能为空(名称或 ID, 可先用 jira_list_transitions 查询)")
        data = _request("GET", f"/issue/{key}/transitions")
        target_id = None
        wanted = transition.strip()
        for item in data.get("transitions") or []:
            if wanted == str(item.get("id")) or wanted.lower() == str(item.get("name") or "").lower():
                target_id = str(item.get("id"))
                break
        if not target_id:
            names = [f"{t.get('id')}={t.get('name')}" for t in (data.get("transitions") or [])]
            raise JiraError(f"未找到流转 {wanted!r}; 可用: {', '.join(names) or '无'}")
        payload: dict[str, Any] = {"transition": {"id": target_id}}
        if resolution.strip():
            payload["fields"] = {"resolution": {"name": resolution.strip()}}
        if comment.strip():
            payload["update"] = {"comment": [{"add": {"body": comment}}]}
        _request("POST", f"/issue/{key}/transitions", payload=payload, ok=(200, 204))
        logger.info(f"issue {issue_key.strip()} 流转 {wanted}")
        return {"ok": True, "transition_id": target_id}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def jira_assign_issue(issue_key: str, assignee: str) -> dict:
    """指派 issue; assignee 传用户名, "me"=当前用户, "-1"=自动指派。"""
    try:
        key = urllib.parse.quote((issue_key or "").strip(), safe="")
        if not key:
            raise JiraError("issue_key 不能为空")
        value = assignee.strip()
        if value == "-1":
            name = None
        elif value.lower() == "me":
            myself = _request("GET", "/myself")
            name = myself.get("name")
            if not name:
                raise JiraError("无法解析当前用户账号")
        else:
            if not value:
                raise JiraError("assignee 不能为空(用户名 / me / -1)")
            name = value
        _request("PUT", f"/issue/{key}/assignee", payload={"name": name}, ok=(200, 204))
        logger.info(f"issue {issue_key.strip()} 指派 {name or '自动'}")
        return {"ok": True, "assignee": name or "自动"}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def jira_delete_issue(issue_key: str, delete_subtasks: bool = False) -> dict:
    """删除 issue(危险: 不可恢复; delete_subtasks=True 同时删除子任务)。"""
    try:
        key = urllib.parse.quote((issue_key or "").strip(), safe="")
        if not key:
            raise JiraError("issue_key 不能为空")
        _request(
            "DELETE", f"/issue/{key}", params={"deleteSubtasks": str(bool(delete_subtasks)).lower()}, ok=(200, 204)
        )
        logger.info(f"删除 issue {issue_key.strip()}")
        return {"ok": True, "deleted": issue_key.strip()}
    except JiraError as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
