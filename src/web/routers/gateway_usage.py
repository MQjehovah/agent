"""算力网关用量代理 Router(web OBO)。

以当前用户的 SSO 会话做 RFC 8693 交换(audience=gateway), 调网关控制台
`GET /api/me/usage` 读本人算力用量(今日/本月 tokens、余额、限流、配额、模型分布),
供 web 个人中心与桌面端展示同一口径:

- GET /api/me/gateway-usage

无 SSO 会话(如本地密码登录)或交换失败返回 401, 前端提示重新登录。
网关控制台地址取 `GATEWAY_ADMIN_URL`(默认内网 http://192.168.31.34:3001)。
"""

import logging
import os

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from web.security import get_authz

logger = logging.getLogger("agent.web.gateway_usage")


def gateway_admin_base() -> str:
    """网关控制台根地址: env GATEWAY_ADMIN_URL > config.json gateway.admin_url > 内网默认。"""
    base = os.environ.get("GATEWAY_ADMIN_URL", "").strip()
    if not base:
        try:
            from settings import get_settings

            base = (get_settings().get("gateway.admin_url", "") or "").strip()
        except Exception:  # noqa: BLE001
            base = ""
    return (base or "http://192.168.31.34:3001").rstrip("/")


def _get_json(url: str, token: str):
    """GET 网关接口(带 Bearer); 网络异常返回 None(由调用方兜底)。"""
    try:
        return requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except Exception as exc:  # noqa: BLE001
        logger.error("[算力用量代理] 请求失败 %s: %s", url, exc)
        return None


def build_gateway_usage_router(server) -> APIRouter:  # noqa: ARG001 (与其它 router 签名一致)
    router = APIRouter()

    @router.get("/api/me/gateway-usage")
    async def gateway_usage(request: Request):
        """代理网关控制台 /api/me/usage(用户身份); 无 SSO 会话/失败返回 401。"""
        try:
            u = get_authz(request)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        uid = int(u.get("uid") or 0)
        base = gateway_admin_base()
        if uid <= 0 or not base:
            return JSONResponse({"error": "无法读取算力用量"}, status_code=503)
        from web.sso_tokens import get_downstream_token

        for attempt in (0, 1):
            token = get_downstream_token(uid, "gateway", force=bool(attempt))
            if not token:
                break
            resp = _get_json(f"{base}/api/me/usage", token)
            if resp is not None and resp.status_code != 401:
                try:
                    payload = resp.json()
                except Exception:  # noqa: BLE001
                    payload = {"error": f"网关返回非 JSON(HTTP {resp.status_code})"}
                return JSONResponse(payload, status_code=resp.status_code)
            logger.info("[算力用量代理] 用户 %s 网关令牌失效(401), 重换重试", uid)
        return JSONResponse(
            {"error": "无企业 SSO 会话或交换失败，无法读取算力用量；请重新登录"},
            status_code=401,
        )

    return router
