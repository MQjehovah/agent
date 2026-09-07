"""SSO/OIDC 登录:JWKS 验 RS256 id_token + 授权码换 token + state/authorize URL 构造。

与 web/server.py 的 HS256 agent JWT(create_jwt/decode_jwt)相互独立。
本模块专门处理外部 SSO(issuer/token/jwks)逻辑,未配置 SSO_ISSUER 视为禁用。

配置优先级: 环境变量(SSO_*) 优先, 其次 config.json 的 "sso" 段(经 settings.get),
最后使用默认值。agent 容器以 env 注入为主(同 router/rag 部署方式)。

依赖: python-jose[cryptography](验签 RS256)、PyJWT(自签测试用,非本模块必需)。
"""

import base64
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from urllib.parse import urlparse
from urllib.request import url2pathname

from jose import exceptions as jose_exceptions
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt

ALGORITHM = "RS256"
JWKS_CACHE_TTL_SECONDS = 300  # JWKS 公钥缓存(与 rag/market 一致, 300s)
STATE_TTL_SECONDS = 300  # SSO state 有效期


class SsoAuthError(Exception):
    """SSO token 校验失败(未配置、JWKS 拉取失败或签名/声明无效)。"""


# ---- 配置读取: env 优先, config.json 'sso' 段兜底 ----
def _cfg(env_key: str, cfg_path: str, default: str = "") -> str:
    """读取一项 SSO 配置。"""
    v = os.environ.get(env_key, "").strip()
    if v:
        return v
    try:
        from settings import get_settings

        v = (get_settings().get(cfg_path, "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    return default


def sso_issuer() -> str:
    return _cfg("SSO_ISSUER", "sso.issuer", "")


def sso_audience() -> str:
    return _cfg("SSO_AUDIENCE", "sso.audience", "agent")


def sso_jwks_uri() -> str:
    return _cfg("SSO_JWKS_URI", "sso.jwks_uri", "")


def sso_client_id() -> str:
    return _cfg("SSO_CLIENT_ID", "sso.client_id", "agent")


def sso_client_secret() -> str:
    return _cfg("SSO_CLIENT_SECRET", "sso.client_secret", "")


def sso_redirect_uri() -> str:
    return _cfg("SSO_REDIRECT_URI", "sso.redirect_uri", "")


def sso_redirect_target() -> str:
    """SSO 回调后浏览器重定向到的前端地址(带 token query)。"""
    return _cfg("SSO_REDIRECT_TARGET", "sso.redirect_target", "/#/login")


def is_configured() -> bool:
    return bool(sso_issuer())


def _resolve_jwks_uri() -> str:
    """JWKS 地址: 显式配置优先, 无则由 issuer 推断。"""
    uri = sso_jwks_uri()
    if uri:
        return uri
    issuer = sso_issuer()
    if issuer:
        return issuer.rstrip("/") + "/.well-known/jwks.json"
    return ""


# ---- JWKS 拉取 + 缓存 ----
_jwks_cache = {"uri": "", "data": None, "fetched_at": 0.0}
_jwks_lock = threading.Lock()


def _read_jwks(uri: str) -> dict:
    """从 uri 读取 JWKS 文档,支持 http(s) 与 file:// 两种 scheme。"""
    scheme = urlparse(uri).scheme
    try:
        if scheme in ("http", "https"):
            with urllib.request.urlopen(uri, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        if scheme == "file":
            path = url2pathname(urlparse(uri).path)
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError) as e:
        raise SsoAuthError(f"拉取 JWKS 失败({uri}): {e}") from e
    raise SsoAuthError(f"不支持的 JWKS URI scheme: {scheme!r}")


def _get_jwks() -> dict:
    """带缓存的 JWKS 获取: TTL 内命中缓存, 过期或换 URI 时加锁重拉。"""
    uri = _resolve_jwks_uri()
    if not uri:
        raise SsoAuthError("SSO not configured")
    cache = _jwks_cache
    now = time.time()
    if (
        cache["uri"] == uri
        and cache["data"] is not None
        and now - cache["fetched_at"] < JWKS_CACHE_TTL_SECONDS
    ):
        return cache["data"]
    with _jwks_lock:
        if (
            cache["uri"] == uri
            and cache["data"] is not None
            and time.time() - cache["fetched_at"] < JWKS_CACHE_TTL_SECONDS
        ):
            return cache["data"]
        data = _read_jwks(uri)
        cache["uri"] = uri
        cache["data"] = data
        cache["fetched_at"] = time.time()
        return data


def _find_key(kid: str | None) -> dict:
    """在 JWKS keys 中定位公钥: 优先按 kid 匹配, 无 kid 时取第一把兜底。"""
    keys = _get_jwks().get("keys") or []
    if not keys:
        raise SsoAuthError("JWKS 文档缺少 keys")
    if kid:
        for item in keys:
            if item.get("kid") == kid:
                return item
        raise SsoAuthError(f"JWKS 中找不到 kid={kid!r}")
    return keys[0]


def _public_key(kid: str | None):
    """把 JWK dict 构造成可验签的公钥对象。"""
    jwk_dict = _find_key(kid)
    return jose_jwk.construct(jwk_dict, algorithm=ALGORITHM)


def verify_sso_token(token: str) -> dict:
    """校验 SSO 签发的 RS256 token, 通过则返回 claims(含 sub 工号)。

    未配置 sso_issuer 视为 SSO 禁用; iss/aud 不匹配、签名无效、过期、
    缺少 sub(工号)等一律抛 SsoAuthError。
    """
    if not sso_issuer():
        raise SsoAuthError("SSO not configured")
    try:
        header = jose_jwt.get_unverified_header(token)
    except jose_exceptions.JWTError as e:
        raise SsoAuthError(f"SSO token header 无效: {e}") from e
    kid = header.get("kid")
    try:
        key = _public_key(kid)
        claims = jose_jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            issuer=sso_issuer(),
            audience=sso_audience() or None,
        )
    except SsoAuthError:
        raise
    except jose_exceptions.JWTError as e:
        raise SsoAuthError(f"SSO token 无效: {e}") from e
    if not claims.get("sub"):
        raise SsoAuthError("SSO token 缺少 sub(工号)")
    return claims


def build_authorize_url(state: str) -> str:
    """构造 SSO OIDC 授权跳转 URL(code + PKCE S256 由 SSO 端处理)。"""
    issuer = sso_issuer()
    if not issuer:
        raise SsoAuthError("SSO not configured")
    params = {
        "response_type": "code",
        "client_id": sso_client_id(),
        "redirect_uri": sso_redirect_uri(),
        "state": state,
        "scope": "openid profile",
    }
    return issuer.rstrip("/") + "/authorize?" + urllib.parse.urlencode(params)


def exchange_code(code: str) -> str:
    """authorization_code → token 交换, 返回 id_token 字符串。

    同时携带 client_secret_basic(Authorization 头) 与 client_secret_post(client_secret 字段),
    兼容两种支持方式。缺 id_token 抛 SsoAuthError。
    """
    issuer = sso_issuer()
    if not issuer:
        raise SsoAuthError("SSO not configured")
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": sso_redirect_uri(),
        "client_id": sso_client_id(),
    }
    secret = sso_client_secret()
    if secret:
        form["client_secret"] = secret
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(issuer.rstrip("/") + "/token", data=data, method="POST")
    if secret:
        token = base64.b64encode(f"{sso_client_id()}:{secret}".encode()).decode("ascii")
        req.add_header("Authorization", "Basic " + token)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise SsoAuthError(f"SSO token 交换失败: {e}") from e
    id_token = payload.get("id_token")
    if not id_token:
        raise SsoAuthError("SSO token 响应缺少 id_token")
    return id_token


# ---- state 管理(内存, TTL 过期, callback 校验后即删)----
_states: dict[str, float] = {}
_states_lock = threading.Lock()


def new_state() -> str:
    """生成一次性 state(随机短串)并记录时间戳。"""
    st = secrets.token_urlsafe(16)
    with _states_lock:
        _states[st] = time.time()
    return st


def validate_state(state: str) -> bool:
    """校验并消费 state: 过期/不存在返回 False。"""
    with _states_lock:
        found = _states.pop(state, None)
    if found is None:
        return False
    return (time.time() - found) <= STATE_TTL_SECONDS


# ---- claims 提取 ----
def sso_user_department(claims: dict) -> str:
    """从 claims 取部门(dept/department)。"""
    return str(claims.get("dept") or claims.get("department") or "")


def sso_user_role(claims: dict) -> str:
    """从 claims 取角色: roles 含 admin → 'admin', 否则 'default'(rub 用户)。"""
    roles = claims.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    lowered = {str(r).lower() for r in roles}
    if "admin" in lowered:
        return "admin"
    return "default"
