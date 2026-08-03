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


def request_with_reauth(method: str, url: str, **kwargs) -> requests.Response:
    """GET/POST；403 时强制重新登录再试一次。"""
    ensure_token()
    kwargs.setdefault("timeout", TIMEOUT)
    resp = requests.request(method, url, **kwargs)
    if resp.status_code == 403 and USERNAME and PASSWORD:
        auto_login(force=True)
        if _token:
            headers = kwargs.get("headers") or {}
            headers = dict(headers)
            headers["token"] = _token
            kwargs["headers"] = headers
            resp = requests.request(method, url, **kwargs)
    return resp


# 模块加载时尝试登录（与原先 ticket_ops 行为一致）
auto_login()
