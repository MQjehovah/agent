"""Web 鉴权公共件：JWT 身份解析 / 角色权限 / 部门数据范围（供各域 Router 复用）。

- /api/auth/* 与 WEBUI_DISABLE_AUTH=1 场景由调用方(Middleware)处理；
- 服务间专用凭证(X-Service-Token)可旁路登录并以 admin 身份放行(网关自动开号场景)。

权限模型：
- ``rbac_roles.permissions`` 为 Web 管理权限键列表(如 admin.users)，``*`` 为通配；
- ``rbac_roles.data_scope`` 为数据可见范围: all(全站) / department(本部门) / self(仅本人)；
- 内置 admin 角色恒拥有全部权限与全站范围(不依赖 DB 行, 老库升级亦安全)。
"""

import logging
import os
from collections.abc import Mapping

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("agent.web.security")

# 平台轨按用户身份开关 MARKET_ACT_AS: 1/true/yes(大小写不敏感)开启, 缺省/其它关闭
_MARKET_ACT_AS_TRUE = frozenset({"1", "true", "yes"})


def market_act_as_enabled(env: Mapping[str, str] | None = None) -> bool:
    """MARKET_ACT_AS 开关: 仅 1/true/yes 为开(缺省关)。"""
    source = os.environ if env is None else env
    return str(source.get("MARKET_ACT_AS") or "").strip().lower() in _MARKET_ACT_AS_TRUE


def resolve_market_act_as(owner: str) -> str:
    """归属 tag/uid → market 用户名(= rbac 工号); 解析失败返回空串并打 WARNING。

    - ``web:{uid}`` 的数字 uid(rbac_users.id) → 查表取 name(= 工号/SSO sub);
    - 非数字(本身已是工号) → 原样使用;
    - 查不到/存储不可用 → 空串(调用方回退服务令牌全量视角, 日志须可见)。
    """
    raw = str(owner or "").strip()
    if not raw:
        return ""
    uid = raw.split(":", 1)[1] if ":" in raw else raw
    if not uid:
        return ""
    if not uid.isdigit():
        return uid  # 已是工号(SSO sub), 直接作为 market 用户名
    try:
        from storage.storage import get_storage  # noqa: PLC0415 — 避免循环导入/启动期依赖
        storage = get_storage()
        if not storage:
            logger.warning(f"MARKET_ACT_AS: 存储不可用, 无法解析 uid={uid} 的市场用户名(回退服务令牌)")
            return ""
        with storage.get_connection() as conn:
            row = conn.execute("SELECT name FROM rbac_users WHERE id = ?", (int(uid),)).fetchone()
        name = str(row["name"]).strip() if row else ""
        if not name:
            logger.warning(f"MARKET_ACT_AS: 未找到 uid={uid} 对应的市场用户名"
                           "(该 worker 回退服务令牌视角)")
            return ""
        return name
    except Exception as e:
        logger.warning(f"MARKET_ACT_AS: 解析 uid={uid} 的市场用户名失败(回退服务令牌视角): {e}")
        return ""


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
    """解析当前请求身份 {uid, name, role}。鉴权已由中间件放行(有效 token / 旁路)。

    主轨为 HS256 agent JWT；兜底为 SSO(RS256) token → 工号 → rbac 用户。
    """
    if os.environ.get("WEBUI_DISABLE_AUTH") == "1":
        return {"uid": 1, "name": "test", "role": "admin"}
    if service_request(request):
        return {"uid": 0, "name": "service", "role": "admin"}
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        raise ValueError("missing bearer")
    cred = h[7:]
    try:
        return _decode_bearer(request)
    except Exception:
        pass
    from web import sso_auth  # noqa: PLC0415
    from web.server import _sso_lookup_user  # noqa: PLC0415 — 避免循环导入
    claims = sso_auth.verify_sso_token(cred)
    user = _sso_lookup_user(claims.get("sub", ""))
    if not user:
        raise ValueError("unprovisioned sso user")
    return {"uid": user["id"], "name": user["name"], "role": user["role"],
            "display_name": user.get("display_name") or ""}


def _resolve_role(role: str, uid) -> tuple[list, str, str]:
    """解析角色权限/数据范围与用户部门；DB 不可用时仅 admin 兜底放行。"""
    admin_fallback = (["*"], "all", "") if role == "admin" else ([], "self", "")
    try:
        from security.rbac import RBACManager  # noqa: PLC0415
        from storage.storage import get_storage  # noqa: PLC0415
        storage = get_storage()
        if not storage:
            return admin_fallback
        rbac = RBACManager(storage)
        perms = rbac.get_permissions(role)
        scope = rbac.get_data_scope(role)
        dept = ""
        if uid not in (None, "", 0, "0", "None") and str(uid).isdigit():
            dept = rbac.get_user_department(int(uid))
        return perms, scope, dept
    except Exception:
        return admin_fallback


def get_authz(request: Request) -> dict:
    """get_auth + 角色权限/数据范围/部门 的组合身份，供细粒度授权使用。"""
    u = dict(get_auth(request))
    perms, scope, dept = _resolve_role(u.get("role", ""), u.get("uid"))
    u["permissions"] = perms
    u["data_scope"] = scope
    u["department"] = dept
    return u


def has_permission(user: dict, perm: str) -> bool:
    """是否拥有某项 Web 权限(admin 角色或 ``*`` 通配恒真)。"""
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    perms = user.get("permissions") or []
    return "*" in perms or perm in perms


def require_perm(request: Request, perm: str) -> dict | None:
    """要求某项权限；非授权返回 None(由调用方决定 403 响应)。"""
    if service_request(request):
        return _service_identity()
    try:
        u = get_authz(request)
    except Exception:
        return None
    return u if has_permission(u, perm) else None


def perm_or_403(request: Request, perm: str) -> tuple[dict | None, JSONResponse | None]:
    """require_perm 的便捷包装：返回 (user|None, response|None)。"""
    if service_request(request):
        return _service_identity(), None
    u = require_perm(request, perm)
    if u is None:
        return None, JSONResponse({"error": "Forbidden"}, status_code=403)
    return u, None


def require_admin(request: Request) -> dict | None:
    """要求超管权限(admin 角色或 permissions 含 ``*``)；非授权返回 None。"""
    return require_perm(request, "*")


def admin_or_403(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """require_admin 的便捷包装：返回 (user|None, response|None)。"""
    return perm_or_403(request, "*")


def scope_department(user: dict) -> str | None:
    """管理类列表的数据范围过滤部门名。

    None = 全站不限(all/admin)；否则限定返回该部门名(空串表示该用户无部门,
    部门范围下不含任何他人数据)。个人(self)范围不会走到管理列表(无对应权限)。
    """
    if not user:
        return ""
    if user.get("role") == "admin" or user.get("data_scope") == "all":
        return None
    return user.get("department") or ""


def _service_identity() -> dict:
    return {"uid": 0, "name": "service", "role": "admin",
            "permissions": ["*"], "data_scope": "all", "department": ""}
