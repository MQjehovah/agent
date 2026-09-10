"""
BMS Ticket MCP Server (rosiwit-cloud)

工单读写：详情 / 改状态 / 评论 / 附件。
设备影子、录包、倒退、回桩见 remote_operation MCP。
鉴权见 cloud_common。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import requests
from mcp.server.fastmcp import FastMCP
from rich.console import Console
from rich.logging import RichHandler

import cloud_common as cloud

console = Console(stderr=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)],
)

logger = logging.getLogger("ticket-ops-mcp")

mcp = FastMCP("Ticket Operations MCP Server")

TICKET_DETAIL_PATH = os.getenv(
    "TICKET_DETAIL_PATH",
    "/rosiwit-cloud/ticket/ops/detail/{ticket_id}",
)
TICKET_STATUS_CHANGE_PATH = os.getenv(
    "TICKET_STATUS_CHANGE_PATH",
    "/rosiwit-cloud/ticket/ops/status/change",
)
TICKET_COMMENT_CREATE_PATH = os.getenv(
    "TICKET_COMMENT_CREATE_PATH",
    "/rosiwit-cloud/ticket/ticketComment/create",
)
TICKET_COMMENT_PAGE_PATH = os.getenv(
    "TICKET_COMMENT_PAGE_PATH",
    "/rosiwit-cloud/ticket/ticketComment/page",
)
TICKET_ATTACHMENT_CREATE_PATH = os.getenv(
    "TICKET_ATTACHMENT_CREATE_PATH",
    "/rosiwit-cloud/ticket/ticketAttachment/create",
)

TICKET_STATUS = {
    1: "已创建",
    2: "处理中",
    3: "已完成",
    4: "已关闭|已拒绝",
    5: "线上运维",
    6: "问题分析",
}

def _request_ticket(ticket_id: str | int) -> dict[str, Any]:
    path = TICKET_DETAIL_PATH.format(ticket_id=ticket_id)
    url = f"{cloud.get_base_url().rstrip('/')}{path}"
    try:
        resp = cloud.request_with_reauth("GET", url, headers=cloud.auth_headers())
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("工单详情请求失败 ticket_id=%s: %s", ticket_id, e)
        return {"success": False, "error": str(e)}


def _change_status(ticket_id: str, status: int) -> dict[str, Any]:
    url = f"{cloud.get_base_url().rstrip('/')}{TICKET_STATUS_CHANGE_PATH}"
    body = {"ticketId": str(ticket_id), "status": int(status)}
    try:
        resp = cloud.request_with_reauth(
            "POST", url, headers=cloud.auth_headers(json_body=True), json=body
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("工单改状态失败 ticket_id=%s status=%s: %s", ticket_id, status, e)
        return {"success": False, "error": str(e)}


def _create_comment(
    ticket_id: str, content: str, parent_id: Optional[str] = None
) -> dict[str, Any]:
    url = f"{cloud.get_base_url().rstrip('/')}{TICKET_COMMENT_CREATE_PATH}"
    body: dict[str, Any] = {
        "ticketId": str(ticket_id),
        "content": content,
        "parentId": parent_id,
    }
    try:
        resp = cloud.request_with_reauth(
            "POST", url, headers=cloud.auth_headers(json_body=True), json=body
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("工单发表评论失败 ticket_id=%s: %s", ticket_id, e)
        return {"success": False, "error": str(e)}


def _normalize_comment_text(text: str) -> str:
    """Normalize whitespace for comment matching."""
    return re.sub(r"\s+", " ", (text or "").strip())


def _comment_content_matches(expected: str, actual: str) -> bool:
    """True if page record content covers the submitted comment.

    Prefer exact / normalized equality. Allow submitted text as substring of
    page content (page may wrap). Do NOT treat page content as substring of
    expected — that false-positives against older shorter comments.
    """
    exp = _normalize_comment_text(expected)
    act = _normalize_comment_text(actual)
    if not exp or not act:
        return False
    if exp == act:
        return True
    if exp in act:
        return True
    # Long comments: first 120 chars of submitted appear in page (truncation)
    if len(exp) > 120 and exp[:120] in act:
        return True
    return False


def _list_comments(
    ticket_id: str, current: int = 1, size: int = 10
) -> dict[str, Any]:
    """POST /ticket/ticketComment/page — GET 会 500，须 POST + ticketId。"""
    url = f"{cloud.get_base_url().rstrip('/')}{TICKET_COMMENT_PAGE_PATH}"
    body: dict[str, Any] = {
        "current": int(current),
        "size": int(size),
        "ticketId": str(ticket_id),
    }
    try:
        resp = cloud.request_with_reauth(
            "POST", url, headers=cloud.auth_headers(json_body=True), json=body
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("工单评论分页失败 ticket_id=%s: %s", ticket_id, e)
        return {"success": False, "error": str(e)}


def _summarize_comment_records(records: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        content = r.get("content")
        content_s = str(content) if content is not None else ""
        out.append(
            {
                "id": r.get("id"),
                "ticketId": r.get("ticketId"),
                "content": content_s,
                "contentPreview": content_s[:200],
                "createTime": r.get("createTime"),
                "createUser": r.get("createUser") or r.get("createUserName"),
                "parentId": r.get("parentId"),
            }
        )
    return out


def _find_matching_comment(
    content: str, records: list[Any]
) -> Optional[dict[str, Any]]:
    for r in records:
        if not isinstance(r, dict):
            continue
        if _comment_content_matches(content, str(r.get("content") or "")):
            return {
                "id": r.get("id"),
                "createTime": r.get("createTime"),
                "contentPreview": str(r.get("content") or "")[:200],
            }
    return None


def _verify_comment_on_page(
    ticket_id: str, content: str, *, size: int = 20
) -> dict[str, Any]:
    """After create, confirm comment appears on ticketComment/page."""
    page = _list_comments(ticket_id, current=1, size=size)
    if not isinstance(page, dict):
        return {
            "verified": False,
            "verifyError": "分页响应格式异常",
            "raw": page,
        }
    ok = bool(page.get("success") or page.get("returnCode") == 200)
    data = page.get("data") if isinstance(page.get("data"), dict) else {}
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        records = []
    if not ok:
        return {
            "verified": False,
            "verifyError": page.get("returnMsg") or page.get("error") or "分页查询失败",
            "returnCode": page.get("returnCode"),
            "total": data.get("total") if isinstance(data, dict) else None,
        }
    match = _find_matching_comment(content, records)
    return {
        "verified": match is not None,
        "verifyError": None if match else "page 未找到与本次 content 匹配的评论",
        "returnCode": page.get("returnCode"),
        "total": data.get("total"),
        "matchedComment": match,
    }


def _create_attachment(
    ticket_id: str, name: str, file_type: str, file_url: str
) -> dict[str, Any]:
    url = f"{cloud.get_base_url().rstrip('/')}{TICKET_ATTACHMENT_CREATE_PATH}"
    body: dict[str, Any] = {
        "ticketId": str(ticket_id),
        "name": name,
        "type": file_type,
        "url": file_url,
    }
    try:
        resp = cloud.request_with_reauth(
            "POST", url, headers=cloud.auth_headers(json_body=True), json=body
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": False, "error": "empty response"}
    except requests.exceptions.RequestException as e:
        logger.error("工单挂附件失败 ticket_id=%s name=%s: %s", ticket_id, name, e)
        return {"success": False, "error": str(e)}


def _summarize(data: dict[str, Any]) -> dict[str, Any]:
    """压缩工单字段，便于 Agent 使用。"""
    faults = []
    for f in data.get("faultList") or []:
        if not isinstance(f, dict):
            continue
        faults.append(
            {
                "id": f.get("id"),
                "faultCode": f.get("faultCode"),
                "faultName": f.get("faultName"),
                "faultContent": f.get("faultContent"),
                "happenTime": f.get("happenTime"),
                "productId": f.get("productId"),
                "deviceId": f.get("deviceId"),
            }
        )
    records = []
    for r in data.get("ticketRecords") or []:
        if not isinstance(r, dict):
            continue
        records.append(
            {
                "id": r.get("id"),
                "preStatus": r.get("preStatus"),
                "currStatus": r.get("currStatus"),
                "currAssign": r.get("currAssign"),
                "remark": r.get("remark"),
                "createTime": r.get("createTime"),
            }
        )
    status = data.get("status")
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    return {
        "id": data.get("id"),
        "code": data.get("code"),
        "name": data.get("name"),
        "status": status,
        "statusName": TICKET_STATUS.get(status_int, f"未知({status})"),
        "priority": data.get("priority"),
        "type": data.get("type"),
        "source": data.get("source"),
        "deviceId": data.get("deviceId"),
        "productId": data.get("productId"),
        "assignUser": data.get("assignUser"),
        "assignUserName": data.get("assignUserName"),
        "description": data.get("description"),
        "createTime": data.get("createTime"),
        "updateTime": data.get("updateTime"),
        "faultList": faults,
        "ticketRecords": records,
        "attachmentCount": len(data.get("attachmentList") or []),
    }


@mcp.tool()
def set_ticket_token(token: str):
    """手动设置 rosiwit-cloud API 的认证 token。

    参数:
    - token: 从云端前端/浏览器请求头复制的 token
    """
    cloud.set_token(token)
    if not cloud.get_token():
        return {"success": False, "error": "token 为空"}
    logger.info("已手动设置工单 token")
    return {"success": True, "message": "token 已设置"}

@mcp.tool()
def list_ticket_statuses():
    """返回 BMS 工单 status 枚举说明。"""
    return {
        "success": True,
        "statuses": [{"status": k, "name": v} for k, v in TICKET_STATUS.items()],
    }

@mcp.tool()
def change_ticket_status(ticket_id: str, status: int):
    """变更 BMS 工单状态。

    对应接口: POST /rosiwit-cloud/ticket/ops/status/change
    Body: {"ticketId": "<id>", "status": <int>}

    状态枚举:
    - 1 已创建
    - 2 处理中
    - 3 已完成
    - 4 已关闭|已拒绝
    - 5 线上运维（AI 接手处置时常用）
    - 6 问题分析（需人工深入分析时）

    参数:
    - ticket_id: 工单 ID，如 215260
    - status: 目标状态码（整数）
    """
    tid = str(ticket_id).strip()
    if not tid:
        return {"success": False, "error": "ticket_id 不能为空"}
    bound = cloud.bind_ticket_cloud(tid)
    if not bound.get("success"):
        return bound
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        return {"success": False, "error": f"status 无效: {status}"}
    if status_int not in TICKET_STATUS:
        return {
            "success": False,
            "error": f"不支持的 status={status_int}",
            "allowed": TICKET_STATUS,
        }

    logger.info(
        "变更工单状态: %s -> %s(%s)",
        tid,
        status_int,
        TICKET_STATUS[status_int],
    )
    result = _change_status(tid, status_int)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}

    ok = bool(result.get("success") or result.get("returnCode") == 200)
    return {
        "success": ok,
        "ticketId": tid,
        "status": status_int,
        "statusName": TICKET_STATUS[status_int],
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }

@mcp.tool()
def list_ticket_comments(ticket_id: str, current: int = 1, size: int = 10):
    """分页查询 BMS 工单评论（写评后核对是否落库）。

    对应接口: POST /rosiwit-cloud/ticket/ticketComment/page
    Body: {"current": 1, "size": 10, "ticketId": "<id>"}
    注意: GET ?current=&size= 会业务 500；必须 POST 且带 ticketId。

    参数:
    - ticket_id: 工单 ID，如 215363
    - current: 页码，默认 1
    - size: 每页条数，默认 10
    """
    tid = str(ticket_id).strip()
    if not tid:
        return {"success": False, "error": "ticket_id 不能为空"}
    bound = cloud.bind_ticket_cloud(tid)
    if not bound.get("success"):
        return bound
    try:
        cur = max(1, int(current))
        sz = max(1, min(100, int(size)))
    except (TypeError, ValueError):
        return {"success": False, "error": "current/size 无效"}

    logger.info("查询工单评论: ticketId=%s current=%s size=%s", tid, cur, sz)
    result = _list_comments(tid, current=cur, size=sz)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}

    ok = bool(result.get("success") or result.get("returnCode") == 200)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        records = []
    return {
        "success": ok,
        "ticketId": tid,
        "current": data.get("current", cur) if isinstance(data, dict) else cur,
        "size": data.get("size", sz) if isinstance(data, dict) else sz,
        "total": data.get("total") if isinstance(data, dict) else None,
        "pages": data.get("pages") if isinstance(data, dict) else None,
        "comments": _summarize_comment_records(records),
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }


_AI_COMMENT_MARKERS = (
    "【AI",
    "【故障",
    "【根因",
    "【诊断",
    "【处置",
    "【验证",
)


def _looks_like_ai_comment(content: str) -> bool:
    text = (content or "").strip()
    if len(text) < 20:
        return False
    head = text[:40]
    return any(m in head for m in _AI_COMMENT_MARKERS)


def _find_existing_ai_comment(records: list[Any]) -> Optional[dict[str, Any]]:
    """在评论分页中找已有 AI 诊断类评论（防跨会话双发）。"""
    for r in records:
        if not isinstance(r, dict):
            continue
        content = str(r.get("content") or "")
        if _looks_like_ai_comment(content):
            return {
                "id": r.get("id"),
                "createTime": r.get("createTime"),
                "contentPreview": content[:200],
                "content": content,
            }
    return None


@mcp.tool()
def create_ticket_comment(ticket_id: str, content: str, parent_id: str = ""):
    """在 BMS 工单下发表评论（AI 分析结论/处理备注写回）。

    对应接口: POST /rosiwit-cloud/ticket/ticketComment/create
    Body: {"ticketId": "<id>", "content": "<text>", "parentId": null}

    写评后会自动 POST ticketComment/page 核对是否落库；
    success=true 仅当 page 能匹配到本次 content（verified=true）。
    对内汇报「💬 评论: 已写回」必须以 verified=true 为依据。

    同一工单若 page 上已有 AI 诊断类评论：不再新建（防双发），返回
    reused_existing=true / verified=true，请只改状态或对内汇报。

    参数:
    - ticket_id: 工单 ID，如 215261
    - content: 评论正文
    - parent_id: 可选，回复某条评论时传入父评论 ID；默认空表示顶级评论
    """
    tid = str(ticket_id).strip()
    text = (content or "").strip()
    if not tid:
        return {"success": False, "error": "ticket_id 不能为空"}
    if not text:
        return {"success": False, "error": "content 不能为空"}
    bound = cloud.bind_ticket_cloud(tid)
    if not bound.get("success"):
        return bound

    pid_raw = (parent_id or "").strip()
    parent: Optional[str] = pid_raw if pid_raw else None

    # 跨会话防双发：page 已有 AI 评论则不落第二条
    page_before = _list_comments(tid, current=1, size=20)
    pre_records: list[Any] = []
    if isinstance(page_before, dict):
        pre_data = page_before.get("data") if isinstance(page_before.get("data"), dict) else {}
        raw = pre_data.get("records") if isinstance(pre_data, dict) else None
        if isinstance(raw, list):
            pre_records = raw
    existing_ai = _find_existing_ai_comment(pre_records)
    if existing_ai is not None:
        existing_body = str(existing_ai.get("content") or "")
        same = _comment_content_matches(text, existing_body)
        logger.warning(
            "工单已有 AI 评论，跳过再次 create ticketId=%s same=%s existingId=%s",
            tid,
            same,
            existing_ai.get("id"),
        )
        return {
            "success": True,
            "verified": True,
            "reused_existing": True,
            "blocked_new_write": not same,
            "apiSuccess": False,
            "ticketId": tid,
            "parentId": parent,
            "matchedComment": {
                "id": existing_ai.get("id"),
                "createTime": existing_ai.get("createTime"),
                "contentPreview": existing_ai.get("contentPreview"),
            },
            "existingComment": {
                "id": existing_ai.get("id"),
                "createTime": existing_ai.get("createTime"),
                "contentPreview": existing_ai.get("contentPreview"),
            },
            "hint": (
                "本工单已有 AI 诊断评论，未再次写入（防双发）。"
                "禁止再调 create_ticket_comment；若尚未结案请仅 change_ticket_status；"
                "对内汇报「💬 评论: 已写回」（沿用已有）。"
            ),
            "error": None,
        }

    logger.info(
        "发表工单评论: ticketId=%s parentId=%s len=%d",
        tid,
        parent,
        len(text),
    )
    result = _create_comment(tid, text, parent)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}

    api_ok = bool(result.get("success") or result.get("returnCode") == 200)
    code = result.get("returnCode")
    msg = result.get("returnMsg") or result.get("error") or ""
    # 云端偶发：评论已落库仍返回 500「服务器业务异常」；同文重试易双写
    ambiguous_500 = (not api_ok) and (
        code == 500
        or code == "500"
        or ("业务异常" in str(msg))
    )

    verify = _verify_comment_on_page(tid, text)
    verified = bool(verify.get("verified"))

    # 以 page 核对为准：API 成功但未落库 → 不算成功；API 500 但已落库 → 算成功
    success = verified
    out: dict[str, Any] = {
        "success": success,
        "verified": verified,
        "apiSuccess": api_ok,
        "ticketId": tid,
        "parentId": parent,
        "returnCode": code,
        "returnMsg": result.get("returnMsg"),
        "data": result.get("data"),
        "matchedComment": verify.get("matchedComment"),
        "commentTotal": verify.get("total"),
        "error": None,
    }
    if verify.get("verifyError") and not verified:
        out["verifyError"] = verify.get("verifyError")

    if success:
        out["error"] = None
        if ambiguous_500:
            out["hint"] = (
                "create 接口曾返回失败，但 ticketComment/page 已核对到本条评论，"
                "视为写评成功。禁止再用相同 content 重试。"
            )
            logger.warning(
                "发表评论 API 失败但 page 已核实 ticketId=%s returnCode=%s",
                tid,
                code,
            )
        return out

    # 未核实到落库
    if ambiguous_500:
        out["possible_persisted"] = False
        out["error"] = msg or "服务器业务异常"
        out["hint"] = (
            "create 返回失败，且 ticketComment/page 未匹配到本次 content。"
            "禁止立刻用相同 content 重试前，可再调 list_ticket_comments 人工核对；"
            "若仍无该条，再考虑重试一次或对内汇报失败。"
        )
        logger.warning(
            "发表评论失败且 page 未核实 ticketId=%s returnCode=%s returnMsg=%s",
            tid,
            code,
            msg,
        )
    elif api_ok:
        out["error"] = (
            verify.get("verifyError")
            or "create 返回成功但 page 未找到本条评论"
        )
        out["hint"] = (
            "禁止立刻用相同 content 重试（可能延迟落库或双写）；"
            "请先 list_ticket_comments 核对后再决定。"
        )
        logger.warning(
            "发表评论 API 成功但 page 未匹配 ticketId=%s",
            tid,
        )
    else:
        out["error"] = msg or result.get("error") or "发表评论失败"
    return out

@mcp.tool()
def create_ticket_attachment(
    ticket_id: str,
    name: str,
    url: str,
    type: str = "",
):
    """将已有 OSS/文件 URL 挂到 BMS 工单附件（云端接口）。

    对应接口: POST /rosiwit-cloud/ticket/ticketAttachment/create
    Body: {"ticketId","name","type","url"}
    例: name=rosbag2_all_....db3, type=\"\", url=https://xz-server.oss-cn-shanghai.aliyuncs.com/...

    说明: 本工具只负责「挂到工单」；若尚无可访问 url，需先有 OSS 地址再调用（当前无自动上传工具）。

    参数:
    - ticket_id: 工单 ID，如 215266
    - name: 附件文件名
    - url: 可访问的文件 URL（OSS 等）
    - type: 前端录包常传空字符串；也可传 MIME
    """
    tid = str(ticket_id).strip()
    file_name = (name or "").strip()
    file_url = (url or "").strip()
    file_type = "" if type is None else str(type).strip()
    if not tid:
        return {"success": False, "error": "ticket_id 不能为空"}
    if not file_name:
        return {"success": False, "error": "name 不能为空"}
    if not file_url:
        return {"success": False, "error": "url 不能为空"}
    bound = cloud.bind_ticket_cloud(tid)
    if not bound.get("success"):
        return bound

    logger.info(
        "挂工单附件: ticketId=%s name=%s type=%s",
        tid,
        file_name,
        file_type,
    )
    result = _create_attachment(tid, file_name, file_type, file_url)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}

    ok = bool(result.get("success") or result.get("returnCode") == 200)
    return {
        "success": ok,
        "ticketId": tid,
        "name": file_name,
        "type": file_type,
        "url": file_url,
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "data": result.get("data"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }

@mcp.tool()
def get_ticket(ticket_id: str):
    """按工单 ID 获取 BMS 工单详情（含 faultList、状态、设备 SN 等）。

    对应接口: GET /rosiwit-cloud/ticket/ops/detail/{ticket_id}
    登录接口: POST /rosiwit-cloud/auth/login (form: userName, password, clientType)

    参数:
    - ticket_id: 工单数字 ID，例如 215261

    返回精简后的工单信息；失败时返回 success=false 与错误信息。
    """
    tid = str(ticket_id).strip()
    if not tid:
        return {"success": False, "error": "ticket_id 不能为空"}

    bound = cloud.bind_ticket_cloud(tid)
    if not bound.get("success"):
        return bound

    logger.info("获取工单详情: %s (base=%s)", tid, cloud.get_base_url())
    result = _request_ticket(tid)

    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}

    if result.get("success") is False and result.get("data") is None:
        return {
            "success": False,
            "returnCode": result.get("returnCode"),
            "returnMsg": result.get("returnMsg") or result.get("error"),
            "hint": "检查 CLOUD_API_BASE_URL/TICKET_API_BASE_URL / 账号密码，或调用 set_ticket_token。",
        }

    data = result.get("data")
    if not isinstance(data, dict):
        return {
            "success": False,
            "returnCode": result.get("returnCode"),
            "returnMsg": result.get("returnMsg", "data 为空或非对象"),
            "raw": result,
        }

    summary = _summarize(data)
    summary["success"] = True
    summary["returnCode"] = result.get("returnCode", 200)
    logger.info(
        "工单 %s code=%s status=%s(%s) deviceId=%s faults=%d",
        summary.get("id"),
        summary.get("code"),
        summary.get("status"),
        summary.get("statusName"),
        summary.get("deviceId"),
        len(summary.get("faultList") or []),
    )
    return summary


if __name__ == "__main__":
    mcp.run()
