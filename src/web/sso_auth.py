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


def sso_downstream_audience() -> str:
    """web OBO 交换的目标受众(下游资源服务器接受的 audience; 内部用户 token 统一 gateway)。

    下游(market/rag/网关)资源轨均已接受 ``gateway``; 可用 ``SSO_DOWNSTREAM_AUDIENCE``
    / config.json ``sso.downstream_audience`` 覆盖。
    """
    return _cfg("SSO_DOWNSTREAM_AUDIENCE", "sso.downstream_audience", "gateway")


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


def verify_sso_token(token: str, *, verify_audience: bool = True, verify_expiry: bool = True) -> dict:
    """校验 SSO 签发的 RS256 token, 通过则返回 claims(含 sub 工号)。

    未配置 sso_issuer 视为 SSO 禁用; iss/aud 不匹配、签名无效、过期、
    缺少 sub(工号)等一律抛 SsoAuthError。
    verify_audience/verify_expiry=False 供「受众/寿命与 agent 不同」的场景(如桌面
    id_token 的 aud=工作台客户端、TTL 很短)跳过对应校验, 签名与 iss 仍强制;
    此类调用须由调用方另行绑定 sub 归属。
    """
    if not sso_issuer():
        raise SsoAuthError("SSO not configured")
    try:
        header = jose_jwt.get_unverified_header(token)
    except jose_exceptions.JWTError as e:
        raise SsoAuthError(f"SSO token header 无效: {e}") from e
    kid = header.get("kid")
    # 关闭 aud 校验必须走 options: 传 audience=None 时 jose 对带 aud 的 token 仍会报 Invalid audience
    options: dict = {}
    if not verify_audience:
        options["verify_aud"] = False
    if not verify_expiry:
        options["verify_exp"] = False
    try:
        key = _public_key(kid)
        claims = jose_jwt.decode(
            token,
            key,
            algorithms=[ALGORITHM],
            issuer=sso_issuer(),
            audience=sso_audience() or None,
            options=options,
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
        "scope": "openid profile offline_access",
    }
    return issuer.rstrip("/") + "/authorize?" + urllib.parse.urlencode(params)


def _post_token_form(form: dict, *, client_id: str | None = None, client_secret: str | None = None) -> dict:
    """POST issuer/token(表单); 同时携带 client_secret_basic 与 client_secret_post, 兼容两种支持方式。

    缺省用 agent 客户端配置; client_id 传空串视为缺省(不覆盖, 与 None 同义),
    client_secret 传空串=显式不带 secret(public 客户端, 如桌面 dashboard-gateway),
    传 None=用配置值。client_id 统一在此写入表单。
    """
    issuer = sso_issuer()
    if not issuer:
        raise SsoAuthError("SSO not configured")
    cid = (client_id or sso_client_id()).strip()
    secret = sso_client_secret() if client_secret is None else client_secret
    payload_form = dict(form)
    payload_form["client_id"] = cid
    if secret:
        payload_form["client_secret"] = secret
    data = urllib.parse.urlencode(payload_form).encode("utf-8")
    req = urllib.request.Request(issuer.rstrip("/") + "/token", data=data, method="POST")
    if secret:
        token = base64.b64encode(f"{cid}:{secret}".encode()).decode("ascii")
        req.add_header("Authorization", "Basic " + token)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise SsoAuthError(f"SSO token 请求失败: {e}") from e


def exchange_code(code: str) -> dict:
    """authorization_code → token 交换, 返回完整 token 组 dict。

    键: id_token(必有) / access_token / refresh_token / expires_in。
    命令式调用方可只用 id_token; web OBO 需保存 refresh_token 供后续刷新与交换。
    缺 id_token 抛 SsoAuthError。
    """
    payload = _post_token_form({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": sso_redirect_uri(),
    })
    if not payload.get("id_token"):
        raise SsoAuthError("SSO token 响应缺少 id_token")
    return payload


def refresh_token_grant(refresh_token: str, *, client_id: str | None = None,
                        client_secret: str | None = None) -> dict:
    """refresh_token grant: 刷新并轮换, 返回新 token 组 dict(缺 id_token 抛错)。

    client_id/client_secret 缺省为 agent 客户端配置; 桌面(dashboard-gateway)托管行由桌面
    客户端独占刷新并复投(单一写者), agent 只读其 id_token, 不再代刷。
    """
    token = (refresh_token or "").strip()
    if not token:
        raise SsoAuthError("refresh_token 为空")
    payload = _post_token_form(
        {"grant_type": "refresh_token", "refresh_token": token},
        client_id=client_id,
        client_secret=client_secret,
    )
    if not payload.get("id_token"):
        raise SsoAuthError("SSO refresh 响应缺少 id_token")
    return payload


def token_exp(token: str) -> float:
    """不验签解析 JWT exp(秒); 失败返回 0。仅用于本地过期判断/缓存, 不用于鉴权。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return float(payload.get("exp") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


# RFC 8693 token exchange 常量
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_ID = "urn:ietf:params:oauth:token-type:id_token"


def exchange_token(subject_token: str, audience: str, *, client_id: str | None = None,
                   client_secret: str | None = None) -> str:
    """RFC 8693 token exchange: 本客户端持有的 subject_token → 目标受众 access_token。

    零号员工代授权用: 把提问者的 SSO token(受众=本客户端) 换成 rag/market 等下游
    受众的短期令牌, 由下游按 subject 权限求交(而非以零号员工服务身份放大)。

    前置(SSO 侧强制): ``subject_token.aud`` 必须等于发起交换的客户端 ``client_id``, 且
    ``audience`` 在该客户端的 ``allowed_audiences`` 白名单内。client_id/client_secret
    缺省为 agent 客户端配置; 托管行按来源客户端覆盖(如桌面 dashboard-gateway,
    public 客户端无 secret, 传空串则不带)。返回 access_token;
    未配置/缺参/交换失败抛 ``SsoAuthError``。
    """
    issuer = sso_issuer()
    if not issuer:
        raise SsoAuthError("SSO not configured")
    sub = (subject_token or "").strip()
    aud = (audience or "").strip()
    if not sub or not aud:
        raise SsoAuthError("token exchange 缺少 subject_token 或 audience")
    cid = (client_id or sso_client_id()).strip()
    secret = sso_client_secret() if client_secret is None else client_secret
    form = {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "subject_token": sub,
        "subject_token_type": TOKEN_TYPE_ID,
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": aud,
        "client_id": cid,
    }
    if secret:
        form["client_secret"] = secret
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(issuer.rstrip("/") + "/token", data=data, method="POST")
    if secret:
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode("ascii")
        req.add_header("Authorization", "Basic " + basic)
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        raise SsoAuthError(f"SSO token exchange 失败: {e}") from e
    access = payload.get("access_token")
    if not access:
        raise SsoAuthError("SSO token exchange 响应缺少 access_token")
    return access



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
    """从 claims 取部门(dept/department); 去首尾空白, 空值返回空串。"""
    return str(claims.get("dept") or claims.get("department") or "").strip()


def sso_user_role(claims: dict) -> str:
    """从 claims 取角色: roles 含 admin → 'admin', 否则 'default'(rub 用户)。"""
    roles = claims.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    lowered = {str(r).lower() for r in roles}
    if "admin" in lowered:
        return "admin"
    return "default"
