"""Web 鉴权公共件：JWT 身份解析 / admin 校验（供各域 Router 复用，与主服务同语义）。

- /api/auth/* 与 WEBUI_DISABLE_AUTH=1 场景由调用方(Middleware)处理；
- 服务间专用凭证(X-Service-Token)可旁路登录并以 admin 身份放行(网关自动开号场景)。
"""

import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("agent.web.security")


def _decode_bearer(request: Request):
    """从 Authorization 头解析并校验 JWT；失败抛异常由调用方决定响应。"""
    from web.server import decode_jwt  # noqa: PLC0415 — 避免循环导入(server 导入 security)
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        raise ValueError("missing bearer")
    return decode_jwt(h[7:])


def service_request(request: Request) -> bool:
    """是否为携带服务间凭证(X-Service-Token)的可信请求。"""
    _svc = os.environ.get("AGENT_SERVICE_TOKEN", "")
    return bool(_svc) and request.headers.get("X-Service-Token") == _svc


def get_auth(request: Request) -> dict:
    """解析当前请求身份 {uid, name, role}。鉴权已由中间件放行(有效 token / 旁路)。"""
    if os.environ.get("WEBUI_DISABLE_AUTH") == "1":
        return {"uid": 1, "name": "test", "role": "admin"}
    if service_request(request):
        return {"uid": 0, "name": "service", "role": "admin"}
    return _decode_bearer(request)


def require_admin(request: Request) -> dict:
    """要求 admin(或服务令牌)；非 admin 返回 403 JSON。"""
    if service_request(request):
        return {"uid": 0, "name": "service", "role": "admin"}
    u = get_auth(request)
    if u.get("role") != "admin":
        return None
    return u


def admin_or_403(request: Request) -> dict:
    """require_admin 的便捷包装：返回 (user|None, response)。None 表示需以 response 结束。"""
    if service_request(request):
        return {"uid": 0, "name": "service", "role": "admin"}, None
    u = get_auth(request)
    if u.get("role") != "admin":
        return None, JSONResponse({"error": "Admin required"}, status_code=403)
    return u, None
