# -*- coding: utf-8 -*-
"""
rosiwit-cloud 共享鉴权与 HTTP 封装。

供 ticket_ops 与 remote_operation 复用。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

logger = logging.getLogger("cloud-common")

# 默认指向 rosiwit-cloud。请设置 TICKET_API_BASE_URL 或 CLOUD_API_BASE_URL。
API_BASE_URL = (
    os.getenv("CLOUD_API_BASE_URL")
    or os.getenv("TICKET_API_BASE_URL")
    or os.getenv("DEVICE_API_BASE_URL")
    or "https://test.rosiwit.com"
)
LOGIN_PATH = os.getenv("TICKET_LOGIN_PATH", "/rosiwit-cloud/auth/login")
USERNAME = os.getenv("TICKET_API_USERNAME") or os.getenv("DEVICE_API_USERNAME", "")
PASSWORD = os.getenv("TICKET_API_PASSWORD") or os.getenv("DEVICE_API_PASSWORD", "")
CLIENT_TYPE = os.getenv("TICKET_CLIENT_TYPE", "WEB")
TIMEOUT = int(os.getenv("TICKET_API_TIMEOUT", "30"))

_token: Optional[str] = os.getenv("TICKET_API_TOKEN") or os.getenv("CLOUD_API_TOKEN") or None


def get_base_url() -> str:
    return API_BASE_URL.rstrip("/")


def get_token() -> Optional[str]:
    return _token


def set_token(token: Optional[str]) -> None:
    global _token
    _token = (token or "").strip() or None


def browser_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    base = get_base_url()
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh_CN",
        "Accept-Timezone": "Asia/Shanghai",
        "Origin": base,
        "Referer": f"{base}/cloud",
    }
    if extra:
        headers.update(extra)
    return headers


def extract_token(result: dict[str, Any]) -> Optional[str]:
    data = result.get("data")
    if isinstance(data, dict):
        token_info = data.get("tokenInfo")
        if isinstance(token_info, dict) and token_info.get("token"):
            return str(token_info["token"])
        if data.get("token"):
            return str(data["token"])
    if result.get("token"):
        return str(result["token"])
    return None


def auto_login(force: bool = False) -> None:
    """使用 rosiwit-cloud 认证中心登录（form-urlencoded，字段名 userName）。"""
    global _token
    if _token and not force:
        return
    if not USERNAME or not PASSWORD:
        logger.warning("CLOUD/TICKET/DEVICE API 账号未配置，跳过自动登录")
        return
    url = f"{get_base_url()}{LOGIN_PATH}"
    try:
        resp = requests.post(
            url,
            data={
                "userName": USERNAME,
                "password": PASSWORD,
                "clientType": CLIENT_TYPE,
            },
            headers=browser_headers(
                {"Content-Type": "application/x-www-form-urlencoded"}
            ),
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json()
        token = extract_token(result) if isinstance(result, dict) else None
        if token and (result.get("success") or result.get("returnCode") == 200):
            _token = token
            logger.info("rosiwit-cloud auth 登录成功 (user=%s)", USERNAME)
        else:
            _token = None
            logger.warning(
                "rosiwit-cloud 登录失败: %s",
                (result or {}).get("returnMsg", "未知错误")
                if isinstance(result, dict)
                else result,
            )
    except Exception as e:
        _token = None
        logger.warning("rosiwit-cloud 登录异常: %s", e)


def auth_headers(json_body: bool = False) -> dict[str, str]:
    headers = browser_headers()
    if json_body:
        headers["Content-Type"] = "application/json"
    if _token:
        headers["token"] = _token
    return headers


def ensure_token() -> None:
    if not _token:
        auto_login(force=True)


def _is_token_invalid_response(resp: requests.Response) -> bool:
    """Detect expired/invalid cloud token (HTTP 403 or business returnCode)."""
    if resp.status_code == 401 or resp.status_code == 403:
        return True
    try:
        body = resp.json()
    except Exception:
        text = (resp.text or "").upper()
        return "TOKEN" in text and ("非法" in (resp.text or "") or "INVALID" in text)
    if not isinstance(body, dict):
        return False
    code = body.get("returnCode")
    if code in (1000001, 401, 403, "1000001", "401", "403"):
        return True
    msg = str(body.get("returnMsg") or body.get("error") or body.get("message") or "")
    upper = msg.upper()
    return ("TOKEN" in upper and ("非法" in msg or "无效" in msg or "INVALID" in upper)) or (
        "未登录" in msg or "登录失效" in msg
    )


def request_with_reauth(method: str, url: str, **kwargs) -> requests.Response:
    """GET/POST；HTTP 403 或业务 TOKEN 非法时强制重新登录再试一次。"""
    ensure_token()
    kwargs.setdefault("timeout", TIMEOUT)
    # Snapshot headers so retry can refresh token without losing other headers
    headers = dict(kwargs.get("headers") or {})
    if _token:
        headers["token"] = _token
    kwargs["headers"] = headers

    resp = requests.request(method, url, **kwargs)
    if _is_token_invalid_response(resp) and USERNAME and PASSWORD:
        logger.info("检测到 token 失效，强制重新登录后重试: %s %s", method, url)
        auto_login(force=True)
        if _token:
            headers = dict(kwargs.get("headers") or {})
            headers["token"] = _token
            kwargs["headers"] = headers
            resp = requests.request(method, url, **kwargs)
    return resp


FILE_UPLOAD_PATH = os.getenv("CLOUD_FILE_UPLOAD_PATH", "/rosiwit-cloud/file/upload")


def upload_file_bytes(
    content: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
) -> dict[str, Any]:
    """上传字节到云端 OSS（multipart POST /rosiwit-cloud/file/upload）。

    成功时 data.url 为可访问地址，可供工单 create_ticket_attachment 使用。
    """
    import io

    name = (filename or "upload.bin").strip() or "upload.bin"
    if not content:
        return {"success": False, "error": "上传内容为空"}
    url = f"{get_base_url()}{FILE_UPLOAD_PATH}"
    files = {"file": (name, io.BytesIO(content), content_type)}
    try:
        # 勿带 json Content-Type，否则 multipart 会失败
        resp = request_with_reauth("POST", url, headers=auth_headers(json_body=False), files=files)
        resp.raise_for_status()
        result = resp.json() if resp.text else {}
    except Exception as e:
        logger.error("云端文件上传失败 name=%s: %s", name, e)
        return {"success": False, "error": str(e)}
    if not isinstance(result, dict):
        return {"success": False, "error": "响应格式异常", "raw": result}
    ok = bool(result.get("success") or result.get("returnCode") == 200)
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    file_url = None
    if isinstance(data, dict):
        file_url = data.get("url") or data.get("fileUrl") or data.get("ossUrl")
    return {
        "success": ok,
        "url": file_url,
        "name": (data or {}).get("name") if isinstance(data, dict) else name,
        "returnCode": result.get("returnCode"),
        "returnMsg": result.get("returnMsg"),
        "data": data,
        "error": None if ok else (result.get("returnMsg") or result.get("error")),
    }


# 模块加载时尝试登录（与原先 ticket_ops 行为一致）
auto_login()
