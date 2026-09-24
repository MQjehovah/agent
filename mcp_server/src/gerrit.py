"""Gerrit MCP Server —— 内网 Gerrit(默认 http://gerrit.xzrobot.com)读写工具。

鉴权:
- GERRIT_USERNAME + GERRIT_HTTP_PASSWORD(默认回退 IT_SYSTEM_PASSWORD), 认证端点走 `/a/` 前缀 + Basic Auth;
  口令来自 Gerrit Settings → HTTP Credentials(不是 LDAP 密码本身);
- 未配置口令时仅匿名只读, 所有写操作返回明确错误。

要点(参考 GerritCodeReview/gerrit-mcp-server 与 cayirtepeomer/gerrit-code-review-mcp):
- 响应可能带 XSSI 防劫持前缀 `)]}'`, 自动剥离;
- diff 文件列表默认排除 lock/二进制/生成物(可用 GERRIT_MCP_EXCLUDE_PATTERNS 覆盖);
- 支持 patchset 对比(base 参数)。

环境变量: GERRIT_URL / GERRIT_USERNAME / GERRIT_HTTP_PASSWORD /
GERRIT_MCP_TIMEOUT(默认30s) / GERRIT_MCP_MAX_OUTPUT_CHARS(默认40000) / GERRIT_MCP_EXCLUDE_PATTERNS。
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

logger = logging.getLogger("mcp.gerrit")

mcp = MCPServer("Rosiwit MCP Server")

_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_SAFE_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
_WRITE_ANNOTATIONS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

DEFAULT_BASE_URL = "http://gerrit.xzrobot.com"
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_OUTPUT_CHARS = 40000
DEFAULT_MAX_DIFF_CHARS = 20000
DEFAULT_EXCLUDE_PATTERNS = (
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$"
    r"|\.(png|jpg|jpeg|gif|ico|pdf|zip|jar|war|so|dll|exe|bin|pyc|class|o|a)$"
)
MAX_QUERY_LIMIT = 100


class GerritError(Exception):
    """Gerrit 调用失败, message 为可读中文原因。"""


# 同步 handler 在 MCP SDK v2 中运行于 anyio worker 线程; 会话与状态用 RLock 串行保护。
_lock = threading.RLock()
_session = requests.Session()

GERRIT_USERNAME = (os.getenv("GERRIT_USERNAME") or "s_software").strip()
_RAW_PASSWORD = (os.getenv("GERRIT_HTTP_PASSWORD") or os.getenv("IT_SYSTEM_PASSWORD") or "").strip()
if _RAW_PASSWORD:
    GERRIT_HTTP_PASSWORD = require_secret("GERRIT_HTTP_PASSWORD", _RAW_PASSWORD) or ""
else:
    GERRIT_HTTP_PASSWORD = ""
    logger.warning("未配置 GERRIT_HTTP_PASSWORD/IT_SYSTEM_PASSWORD: 仅匿名只读, 写操作不可用")


def _base_url() -> str:
    return (os.getenv("GERRIT_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL).rstrip("/")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(str(os.getenv(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _timeout() -> int:
    return _env_int("GERRIT_MCP_TIMEOUT", DEFAULT_TIMEOUT)


def _max_output_chars() -> int:
    return _env_int("GERRIT_MCP_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS)


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


def _credentials() -> tuple[str, str] | None:
    if GERRIT_HTTP_PASSWORD:
        return (GERRIT_USERNAME, GERRIT_HTTP_PASSWORD)
    return None


def _require_write() -> None:
    if not GERRIT_HTTP_PASSWORD:
        raise GerritError(
            "Gerrit 写操作需要凭证: 请设置 GERRIT_HTTP_PASSWORD(或 IT_SYSTEM_PASSWORD), "
            "口令在 Gerrit Settings → HTTP Credentials 生成"
        )


def _encode_change_id(change_id: str) -> str:
    """change id 支持数字/`project~branch~Ihash`; 数字与 `~` 原样, 其余 URL 编码。"""
    value = (change_id or "").strip()
    if not value:
        raise GerritError("change_id 不能为空(数字、Change-Id 或 project~branch~Change-Id)")
    if value.isdigit():
        return value
    return urllib.parse.quote(value, safe="~")


def _encode_file(file_path: str) -> str:
    value = (file_path or "").strip().lstrip("/")
    if not value:
        raise GerritError("file 不能为空")
    return urllib.parse.quote(value, safe="")


def _exclude_patterns() -> re.Pattern:
    raw = (os.getenv("GERRIT_MCP_EXCLUDE_PATTERNS") or "").strip()
    pattern = raw or DEFAULT_EXCLUDE_PATTERNS
    try:
        return re.compile(pattern)
    except re.error:
        logger.warning("GERRIT_MCP_EXCLUDE_PATTERNS 非法正则, 已回退默认排除规则")
        return re.compile(DEFAULT_EXCLUDE_PATTERNS)


def _is_excluded(file_path: str) -> bool:
    return bool(_exclude_patterns().search(file_path or ""))


def _strip_xssi(text: str) -> str:
    """剥离 Gerrit 响应的 XSSI 前缀 `)]}'`(可能独占一行)。"""
    value = (text or "").lstrip("\ufeff")
    if value.startswith(")]}'"):
        value = value[4:]
        if value.startswith("\n"):
            value = value[1:]
    return value


def _describe_error(resp: requests.Response) -> str:
    detail = _strip_xssi(resp.text or "").strip().replace("\n", " ")[:300]
    reason = f"Gerrit API HTTP {resp.status_code}"
    return f"{reason}: {detail}" if detail else reason


def _request_json(method: str, path: str, *, params=None, payload=None, ok=(200,)):
    """统一请求入口(测试 mock 点): 认证前缀 + XSSI 剥离 + 状态码校验 + JSON 解析。"""
    with _lock:
        prefix = "/a" if _credentials() else ""
        url = f"{_base_url()}{prefix}{path}"
        headers = {"Accept": "application/json"}
        try:
            resp = _session.request(
                method, url, params=params, json=payload, headers=headers, auth=_credentials(), timeout=_timeout()
            )
        except requests.RequestException as exc:
            raise GerritError(f"Gerrit 请求失败: {exc.__class__.__name__}") from None
        if resp.status_code not in ok:
            raise GerritError(_describe_error(resp))
        text = _strip_xssi(resp.text or "")
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except ValueError:
            raise GerritError("Gerrit 返回非 JSON 内容") from None


def _change_summary(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "number": item.get("_number"),
        "project": item.get("project"),
        "branch": item.get("branch"),
        "subject": item.get("subject"),
        "status": item.get("status"),
        "owner": (item.get("owner") or {}).get("name") or (item.get("owner") or {}).get("username"),
        "updated": item.get("updated"),
        "insertions": item.get("insertions"),
        "deletions": item.get("deletions"),
        "current_revision": item.get("current_revision"),
        "current_revision_number": item.get("current_revision_number"),
        "submittable": item.get("submittable"),
        "wip": item.get("work_in_progress"),
        "url": item.get("_url") or f"/c/{item.get('project')}/+/{item.get('_number')}",
    }


def _diff_text(content: Any) -> str:
    """Gerrit diff JSON(content 数组) → '+'/'-'/' ' 文本。"""
    lines: list[str] = []
    for chunk in content or []:
        if not isinstance(chunk, dict):
            continue
        for line in chunk.get("ab") or []:
            lines.append(f" {line}")
        for line in chunk.get("a") or []:
            lines.append(f"-{line}")
        for line in chunk.get("b") or []:
            lines.append(f"+{line}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 读工具

@mcp.tool(annotations=_READ_ANNOTATIONS)
def gerrit_query_changes(query: str, limit: int = 25, start: int = 0) -> dict:
    """按 Gerrit 查询语法搜索 change(如 status:open project:cloud/xz-data owner:self)。"""
    try:
        if not query.strip():
            raise GerritError("query 不能为空(如 status:open project:xxx)")
        limit = _clamp_int(limit, 1, MAX_QUERY_LIMIT, 25)
        start = _clamp_int(start, 0, 100000, 0)
        params = [
            ("q", query.strip()),
            ("n", limit),
            ("S", start),
            ("o", "CURRENT_REVISION"),
            ("o", "CURRENT_COMMIT"),
        ]
        items = _request_json("GET", "/changes/", params=params)
        more = bool(items and isinstance(items[-1], dict) and items[-1].get("_more_changes"))
        return {
            "ok": True,
            "changes": [_change_summary(item) for item in items],
            "count": len(items),
            "start": start,
            "has_more": more,
        }
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gerrit_get_change(change_id: str) -> dict:
    """获取 change 详情(含标签投票、可提交状态、评审人、变更消息)。"""
    try:
        params = [
            ("o", "CURRENT_REVISION"),
            ("o", "CURRENT_COMMIT"),
            ("o", "DETAILED_LABELS"),
            ("o", "DETAILED_ACCOUNTS"),
            ("o", "MESSAGES"),
            ("o", "REVIEWERS"),
            ("o", "SUBMITTABLE"),
        ]
        data = _request_json("GET", f"/changes/{_encode_change_id(change_id)}", params=params)
        labels = {}
        for name, label in (data.get("labels") or {}).items():
            labels[name] = {
                "value": label.get("value"),
                "approved": label.get("approved"),
                "rejected": label.get("rejected"),
                "recommended": label.get("recommended"),
                "disliked": label.get("disliked"),
                "blocking": label.get("blocking"),
            }
        messages = []
        for msg in data.get("messages") or []:
            body, _ = _truncate(str(msg.get("message") or ""), 2000)
            messages.append(
                {
                    "author": (msg.get("author") or {}).get("name") or (msg.get("author") or {}).get("username"),
                    "date": msg.get("date"),
                    "revision_number": msg.get("_revision_number"),
                    "message": body,
                }
            )
        result = _change_summary(data)
        result.update(
            {
                "ok": True,
                "labels": labels,
                "reviewers": [
                    {"name": (r.get("name") or r.get("username")), "state": r.get("state")}
                    for r in ((data.get("reviewers") or {}).get("REVIEWER") or [])
                ],
                "cc": [
                    {"name": (r.get("name") or r.get("username"))}
                    for r in ((data.get("reviewers") or {}).get("CC") or [])
                ],
                "messages": messages,
                "current_revision_id": (data.get("current_revision") or ""),
            }
        )
        return result
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gerrit_list_files(change_id: str, revision: str = "current") -> dict:
    """列出 change 某修订修改的文件(默认 current; 排除二进制/lock 等, 见 excluded 字段)。"""
    try:
        data = _request_json("GET", f"/changes/{_encode_change_id(change_id)}/revisions/{revision}/files/")
        files = []
        excluded = []
        for path, meta in (data or {}).items():
            if path.startswith("/"):
                continue
            item = {
                "path": path,
                "status": (meta or {}).get("status"),
                "lines_inserted": (meta or {}).get("lines_inserted"),
                "lines_deleted": (meta or {}).get("lines_deleted"),
                "size": (meta or {}).get("size"),
            }
            if _is_excluded(path):
                excluded.append(path)
            else:
                files.append(item)
        return {"ok": True, "revision": revision, "files": files, "excluded": excluded}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gerrit_get_file_diff(
    change_id: str,
    file: str,
    revision: str = "current",
    base: str = "",
    max_chars: int = DEFAULT_MAX_DIFF_CHARS,
) -> dict:
    """获取单文件 diff; base 传 patchset 号可与 revision 对比(如 base=1, revision=current)。"""
    try:
        if _is_excluded(file):
            return {"ok": True, "file": file, "excluded": True, "diff": "", "note": "该文件命中排除规则(二进制/lock)"}
        max_chars = _clamp_int(max_chars, 500, _max_output_chars(), DEFAULT_MAX_DIFF_CHARS)
        params = {"base": base.strip()} if base.strip() else None
        data = _request_json(
            "GET",
            f"/changes/{_encode_change_id(change_id)}/revisions/{revision}/files/{_encode_file(file)}/diff",
            params=params,
        )
        text = _diff_text(data.get("content"))
        diff, truncated = _truncate(text, max_chars)
        return {
            "ok": True,
            "file": file,
            "revision": revision,
            "base": base.strip() or "",
            "diff": diff,
            "truncated": truncated,
            "meta_a": (data.get("meta_a") or {}).get("name"),
            "meta_b": (data.get("meta_b") or {}).get("name"),
        }
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gerrit_get_commit_message(change_id: str, revision: str = "current") -> dict:
    """获取指定修订的完整 commit message。"""
    try:
        data = _request_json("GET", f"/changes/{_encode_change_id(change_id)}/revisions/{revision}/commit")
        message, truncated = _truncate(str(data.get("message") or ""), _max_output_chars())
        return {"ok": True, "commit": data.get("commit"), "subject": data.get("subject"), "message": message, "truncated": truncated}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gerrit_list_comments(change_id: str, revision: str = "") -> dict:
    """列出 change 评论(默认所有修订; 给 revision 则只看该修订)。"""
    try:
        if revision.strip():
            path = f"/changes/{_encode_change_id(change_id)}/revisions/{revision.strip()}/comments"
        else:
            path = f"/changes/{_encode_change_id(change_id)}/comments"
        data = _request_json("GET", path)
        comments = []
        for file_path, items in (data or {}).items():
            for item in items or []:
                body, _ = _truncate(str(item.get("message") or ""), 2000)
                comments.append(
                    {
                        "file": file_path,
                        "line": item.get("line"),
                        "author": (item.get("author") or {}).get("name") or (item.get("author") or {}).get("username"),
                        "patch_set": item.get("patch_set"),
                        "updated": item.get("updated"),
                        "unresolved": item.get("unresolved"),
                        "message": body,
                    }
                )
        return {"ok": True, "comments": comments, "count": len(comments)}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_READ_ANNOTATIONS)
def gerrit_related_changes(change_id: str) -> dict:
    """查看 change 的关联链(父子依赖/同链变更), 用于判断依赖关系。"""
    try:
        data = _request_json("GET", f"/changes/{_encode_change_id(change_id)}/revisions/current/related")
        return {
            "ok": True,
            "changes": [_change_summary(item) for item in (data.get("changes") or [])],
            "default": bool(data.get("default")),
        }
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- 写工具

@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gerrit_add_reviewer(change_id: str, reviewer: str, state: str = "REVIEWER") -> dict:
    """添加评审人/抄送(state=REVIEWER 或 CC)。reviewer 支持账号/邮箱。"""
    try:
        _require_write()
        state = state.strip().upper() or "REVIEWER"
        if state not in ("REVIEWER", "CC"):
            raise GerritError("state 仅支持 REVIEWER 或 CC")
        data = _request_json(
            "POST",
            f"/changes/{_encode_change_id(change_id)}/reviewers",
            payload={"reviewer": reviewer.strip(), "state": state},
        )
        logger.info(f"change {change_id} 添加 {state} {reviewer}")
        return {"ok": True, "reviewers": [r.get("name") or r.get("username") for r in (data.get("reviewers") or [])]}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gerrit_set_topic(change_id: str, topic: str) -> dict:
    """设置 change 主题(topic 传空字符串则删除主题)。"""
    try:
        _require_write()
        _request_json("PUT", f"/changes/{_encode_change_id(change_id)}/topic", payload={"topic": topic.strip()})
        logger.info(f"change {change_id} 设置 topic={topic!r}")
        return {"ok": True, "topic": topic.strip()}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gerrit_set_wip(change_id: str, wip: bool = True) -> dict:
    """标记 change 为 WIP(wip=true)或重新可评审(wip=false)。"""
    try:
        _require_write()
        action = "wip" if wip else "ready"
        _request_json("POST", f"/changes/{_encode_change_id(change_id)}/{action}")
        logger.info(f"change {change_id} {action}")
        return {"ok": True, "wip": bool(wip)}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_SAFE_WRITE_ANNOTATIONS)
def gerrit_restore_change(change_id: str, message: str = "") -> dict:
    """恢复被 abandon 的 change 重新评审。"""
    try:
        _require_write()
        payload = {"message": message} if message.strip() else {}
        _request_json("POST", f"/changes/{_encode_change_id(change_id)}/restore", payload=payload)
        logger.info(f"change {change_id} restore")
        return {"ok": True, "restored": True}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------- 危险写工具

@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def gerrit_set_review(
    change_id: str,
    message: str = "",
    labels: dict | None = None,
    inline_comments: list | None = None,
    notify: str = "OWNER",
) -> dict:
    """提交评审(投票+评论)。labels 如 {"Code-Review": 2}; inline_comments 为
    [{"file": "a/b.py", "line": 42, "message": "...", "side": "REVISION"}]。"""
    try:
        _require_write()
        payload: dict[str, Any] = {}
        if message.strip():
            payload["message"] = message
        if labels:
            payload["labels"] = labels
        if inline_comments:
            comments: dict[str, list] = {}
            for item in inline_comments:
                if not isinstance(item, dict):
                    raise GerritError("inline_comments 每项必须是 {file, line, message} 字典")
                file_path = str(item.get("file") or "").strip()
                if not file_path:
                    raise GerritError("inline_comments 缺少 file")
                entry: dict[str, Any] = {"message": str(item.get("message") or ""), "line": int(item.get("line") or 0)}
                if item.get("side"):
                    entry["side"] = str(item["side"]).upper()
                comments.setdefault(file_path, []).append(entry)
            payload["comments"] = comments
        if not payload:
            raise GerritError("至少提供 message/labels/inline_comments 之一")
        notify = notify.strip().upper() or "OWNER"
        if notify not in ("NONE", "OWNER", "OWNER_REVIEWERS", "ALL"):
            raise GerritError("notify 仅支持 NONE/OWNER/OWNER_REVIEWERS/ALL")
        payload["notify"] = notify
        _request_json("POST", f"/changes/{_encode_change_id(change_id)}/review", payload=payload)
        logger.info(f"change {change_id} 提交评审 labels={labels}")
        return {"ok": True, "labels": labels or {}, "comment_count": len(inline_comments or [])}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def gerrit_submit_change(change_id: str) -> dict:
    """提交(合入)change(危险: 直接进入目标分支)。"""
    try:
        _require_write()
        _request_json("POST", f"/changes/{_encode_change_id(change_id)}/submit", payload={})
        logger.info(f"change {change_id} submit")
        return {"ok": True, "submitted": True}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_WRITE_ANNOTATIONS)
def gerrit_abandon_change(change_id: str, message: str = "") -> dict:
    """弃用 change(危险; 可用 gerrit_restore_change 恢复)。"""
    try:
        _require_write()
        payload = {"message": message} if message.strip() else {}
        _request_json("POST", f"/changes/{_encode_change_id(change_id)}/abandon", payload=payload)
        logger.info(f"change {change_id} abandon")
        return {"ok": True, "abandoned": True}
    except GerritError as exc:
        return {"ok": False, "error": str(exc)}


if __name__ == "__main__":
    logger.info("启动 MCP Server")
    mcp.run()
