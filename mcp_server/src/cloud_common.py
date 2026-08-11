# -*- coding: utf-8 -*-
"""
rosiwit-cloud 共享鉴权与 HTTP 封装。

供 ticket_ops 与 remote_operation 复用。

多环境：设置 TICKET_API_BASE_URLS（逗号分隔）后，按 ticket_id 探测所在云并缓存；
单环境仍用 CLOUD_API_BASE_URL / TICKET_API_BASE_URL。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Optional

import requests

logger = logging.getLogger("cloud-common")

# 默认指向 rosiwit-cloud。多环境部署请用环境变量覆盖（勿写死在代码里）：
# CLOUD_API_BASE_URL / TICKET_API_BASE_URL
#   test → https://test.rosiwit.com
#   cn   → https://bms-cn.xzrobot.com
#   eu   → https://bms-eu.rosiwit.com
#   na   → https://bms-na.rosiwit.com
# 单机多云（按钮同一 URL）：TICKET_API_BASE_URLS=上述四个逗号拼接
API_BASE_URL = (
    os.getenv("CLOUD_API_BASE_URL")
    or os.getenv("TICKET_API_BASE_URL")
    or os.getenv("DEVICE_API_BASE_URL")
    or "https://test.rosiwit.com"
)
LOGIN_PATH = os.getenv("TICKET_LOGIN_PATH", "/rosiwit-cloud/auth/login")
TICKET_DETAIL_PATH_TMPL = os.getenv(
    "TICKET_DETAIL_PATH",
    "/rosiwit-cloud/ticket/ops/detail/{ticket_id}",
)
USERNAME = os.getenv("TICKET_API_USERNAME") or os.getenv("DEVICE_API_USERNAME", "")
PASSWORD = os.getenv("TICKET_API_PASSWORD") or os.getenv("DEVICE_API_PASSWORD", "")
CLIENT_TYPE = os.getenv("TICKET_CLIENT_TYPE", "WEB")
TIMEOUT = int(os.getenv("TICKET_API_TIMEOUT", "30"))
# ticket_ops / remote_operation 分进程，用文件同步当前云
BIND_STATE_PATH = os.getenv(
    "CLOUD_BIND_STATE_PATH",
    "/tmp/agent_cloud_bind.json",
)

_lock = threading.RLock()
_active_base: str = API_BASE_URL.rstrip("/")
_token_by_base: dict[str, Optional[str]] = {}
_ticket_base_cache: dict[str, str] = {}

_seed = os.getenv("TICKET_API_TOKEN") or os.getenv("CLOUD_API_TOKEN")
if _seed:
    _token_by_base[_active_base] = _seed.strip() or None


def configured_base_urls() -> list[str]:
    """返回候选 Base URL 列表（多云或单云）。"""
    raw = (
        os.getenv("TICKET_API_BASE_URLS")
        or os.getenv("CLOUD_API_BASE_URLS")
        or ""
    ).strip()
    if raw:
        urls = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
        if urls:
            return urls
    return [API_BASE_URL.rstrip("/")]


def _write_bind_state(ticket_id: str | None = None) -> None:
    payload = {
        "baseUrl": _active_base.rstrip("/"),
        "ticketId": (ticket_id or "").strip() or None,
    }
    try:
        parent = os.path.dirname(BIND_STATE_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(BIND_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as e:
        logger.warning("写入云绑定状态失败 path=%s: %s", BIND_STATE_PATH, e)


def _load_bind_state() -> None:
    """从共享文件刷新 _active_base（供 remote_operation 等进程跟随 ticket_ops）。"""
    global _active_base
    try:
        if not os.path.isfile(BIND_STATE_PATH):
            return
        with open(BIND_STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        base = str((data or {}).get("baseUrl") or "").strip().rstrip("/")
        if base and base != _active_base:
            _active_base = base
            logger.info("从绑定状态加载云 Base URL: %s", base)
    except Exception as e:
        logger.debug("读取云绑定状态失败: %s", e)


def get_base_url() -> str:
    with _lock:
        if len(configured_base_urls()) > 1:
            _load_bind_state()
        return _active_base.rstrip("/")


def set_active_base(url: str, ticket_id: str | None = None) -> None:
    global _active_base
    base = (url or "").strip().rstrip("/")
    if not base:
        return
    with _lock:
        _active_base = base
        _write_bind_state(ticket_id)

def get_token() -> Optional[str]:
    with _lock:
        return _token_by_base.get(get_base_url())


def set_token(token: Optional[str]) -> None:
    value = (token or "").strip() or None
    with _lock:
        _token_by_base[get_base_url()] = value


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
    base = get_base_url()
    with _lock:
        existing = _token_by_base.get(base)
    if existing and not force:
        return
    if not USERNAME or not PASSWORD:
        logger.warning("CLOUD/TICKET/DEVICE API 账号未配置，跳过自动登录")
        return
    url = f"{base}{LOGIN_PATH}"
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
            set_token(token)
            logger.info("rosiwit-cloud auth 登录成功 (base=%s user=%s)", base, USERNAME)
        else:
            set_token(None)
            logger.warning(
                "rosiwit-cloud 登录失败 base=%s: %s",
                base,
                (result or {}).get("returnMsg", "未知错误")
                if isinstance(result, dict)
                else result,
            )
    except Exception as e:
        set_token(None)
        logger.warning("rosiwit-cloud 登录异常 base=%s: %s", base, e)


def auth_headers(json_body: bool = False) -> dict[str, str]:
    headers = browser_headers()
    if json_body:
        headers["Content-Type"] = "application/json"
    token = get_token()
    if token:
        headers["token"] = token
    return headers


def ensure_token() -> None:
    if not get_token():
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
    headers = dict(kwargs.get("headers") or {})
    token = get_token()
    if token:
        headers["token"] = token
    kwargs["headers"] = headers

    resp = requests.request(method, url, **kwargs)
    if _is_token_invalid_response(resp) and USERNAME and PASSWORD:
        logger.info("检测到 token 失效，强制重新登录后重试: %s %s", method, url)
        auto_login(force=True)
        token = get_token()
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers["token"] = token
            kwargs["headers"] = headers
            resp = requests.request(method, url, **kwargs)
    return resp


def _ticket_detail_ok(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if body.get("success") is False and body.get("data") is None:
        return False
    data = body.get("data")
    return isinstance(data, dict) and bool(data.get("id") or data.get("code") or data)


def resolve_base_for_ticket(ticket_id: str) -> Optional[str]:
    """按 ticket_id 探测并绑定所在云；结果缓存。单云时直接绑定默认 URL。"""
    tid = str(ticket_id or "").strip()
    if not tid:
        return None

    with _lock:
        cached = _ticket_base_cache.get(tid)
    if cached:
        set_active_base(cached, ticket_id=tid)
        ensure_token()
        return cached

    urls = configured_base_urls()
    if len(urls) == 1:
        set_active_base(urls[0], ticket_id=tid)
        ensure_token()
        with _lock:
            _ticket_base_cache[tid] = urls[0]
        return urls[0]

    last_error: Optional[str] = None
    for base in urls:
        set_active_base(base, ticket_id=tid)
        auto_login(force=False)
        path = TICKET_DETAIL_PATH_TMPL.format(ticket_id=tid)
        url = f"{base}{path}"
        try:
            resp = request_with_reauth("GET", url, headers=auth_headers())
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            body = resp.json() if resp.text else {}
            if _ticket_detail_ok(body):
                with _lock:
                    _ticket_base_cache[tid] = base
                set_active_base(base, ticket_id=tid)
                logger.info("工单 %s 定位到云: %s", tid, base)
                return base
            last_error = str(
                (body or {}).get("returnMsg")
                or (body or {}).get("error")
                or f"status={resp.status_code}"
            )
        except Exception as e:
            last_error = str(e)
            logger.info("探测工单 %s @ %s 失败: %s", tid, base, e)
            continue

    logger.warning("工单 %s 未在任何云找到 (tried=%s last=%s)", tid, urls, last_error)
    return None

def bind_ticket_cloud(ticket_id: str) -> dict[str, Any]:
    """供 ticket 工具调用：绑定云环境。失败返回 success=false。"""
    tid = str(ticket_id or "").strip()
    if not tid:
        return {"success": False, "error": "ticket_id 不能为空"}
    base = resolve_base_for_ticket(tid)
    if not base:
        return {
            "success": False,
            "error": f"未能在已配置云环境中找到工单 {tid}",
            "tried": configured_base_urls(),
            "hint": "检查 TICKET_API_BASE_URLS / 账号，或确认 ticket_id 正确",
        }
    return {"success": True, "ticketId": tid, "baseUrl": base}


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


# 模块加载时尝试登录默认云（与原先 ticket_ops 行为一致）
auto_login()
