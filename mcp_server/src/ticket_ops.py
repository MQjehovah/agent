"""
BMS Ticket MCP Server (rosiwit-cloud)

工单读写：详情 / 改状态 / 评论 / 附件。
设备影子、录包、倒退、回桩见 remote_operation MCP。
鉴权见 cloud_common。
"""
from __future__ import annotations

import logging
import os
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
def create_ticket_comment(ticket_id: str, content: str, parent_id: str = ""):
    """在 BMS 工单下发表评论（AI 分析结论/处理备注写回）。

    对应接口: POST /rosiwit-cloud/ticket/ticketComment/create
    Body: {"ticketId": "<id>", "content": "<text>", "parentId": null}

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

    pid_raw = (parent_id or "").strip()
    parent: Optional[str] = pid_raw if pid_raw else None

    logger.info(
        "发表工单评论: ticketId=%s parentId=%s len=%d",
        tid,
        parent,
        len(text),
    )
    result = _create_comment(tid, text, parent)
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}

    ok = bool(result.get("success") or result.get("returnCode") == 200)
    return {
        "success": ok,
        "ticketId": tid,
        "parentId": parent,
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "data": result.get("data"),
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }

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
