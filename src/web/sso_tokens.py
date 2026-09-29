"""按用户的 SSO token 托管与下游代换(web OBO, RFC 8693)。

登录回调/桌面复投把用户 id_token(约 10 分钟)/refresh_token 存入本地存储, 本模块负责:

- 自家客户端(web 回调, client_id == ``sso_auth.sso_client_id()``)行: id_token 临近过期时
  用 refresh_token 刷新(轮换后回存), 刷新失败清空托管;
- 桌面(dashboard-gateway)来源的行: 单一写者——由桌面客户端独占刷新并复投, agent 只读其
  id_token(留 60s 余量), 已过期返回空串(不发起刷新、不删除托管行), 由调用方 fail-closed
  引导登录;
- 按受众交换下游 token(默认 1 小时), 进程内按 (uid, audience) 缓存;
- 用户未走 SSO(如本地密码登录)或托管缺失时返回空串, 由调用方 fail-closed 处理
  (统一引导登录, 不回退服务身份);
- 需要 fail-closed 的调用方用 ``require_user_token``: 无托管 token 时抛
  ``UserTokenUnavailable``(统一引导登录文案), 不得回退服务身份。

受众默认 ``gateway``(内部用户 token 统一受众; market/rag/网关资源轨均接受), 可用
``SSO_DOWNSTREAM_AUDIENCE`` / config.json ``sso.downstream_audience`` 覆盖。
"""

import logging
import threading
import time

from web import sso_auth

logger = logging.getLogger("agent.web.sso_tokens")

_REFRESH_MARGIN_SECONDS = 120.0  # id_token 剩余寿命低于该值即刷新
_DESKTOP_READ_MARGIN_SECONDS = 60.0  # 桌面行只读 id_token 的余量(单一写者, 过期即 fail-closed)
_EXCHANGE_MARGIN_SECONDS = 60.0  # 下游 token 剩余寿命低于该值视为过期

_lock = threading.Lock()
_cache: dict[tuple[int, str], tuple[str, float]] = {}


def _storage():
    from storage.storage import get_storage

    return get_storage()


def _forget_locked(uid: int) -> None:
    for key in [k for k in _cache if k[0] == uid]:
        _cache.pop(key, None)


def _client_creds(row: dict) -> tuple[str, str | None]:
    """按托管行来源客户端返回实际执行用的 (client_id, client_secret)。

    自家客户端(配置的 sso_client_id, 或旧实现遗留的字面 'agent')统一解析为配置值并
    交默认 secret(自定义 SSO_CLIENT_ID 部署下遗留行也能按配置客户端执行);
    其它客户端(桌面 dashboard-gateway 等 public 客户端)按原值且不带 secret。
    """
    cid = str(row.get("client_id") or "agent").strip() or "agent"
    if cid == sso_auth.sso_client_id() or cid == "agent":
        return sso_auth.sso_client_id(), None
    return cid, ""


def save_user_tokens(uid: int, tokens: dict) -> bool:
    """SSO 回调/桌面托管: 保存 id_token/refresh_token(幂等覆盖), 失败返回 False(不阻断登录)。

    tokens.client_id 记录来源客户端(缺省取本部署 sso_client_id); 后续刷新/交换按它执行。
    """
    if uid <= 0 or not isinstance(tokens, dict):
        return False
    id_token = str(tokens.get("id_token") or "")
    if not id_token:
        return False
    refresh_token = str(tokens.get("refresh_token") or "")
    client_id = str(tokens.get("client_id") or "").strip() or sso_auth.sso_client_id()
    exp = float(sso_auth.token_exp(id_token) or 0)
    storage = _storage()
    if storage is None:
        return False
    try:
        storage.save_sso_tokens(uid, id_token, refresh_token, exp, client_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"保存用户 {uid} SSO token 失败: {e}")
        return False
    with _lock:
        _forget_locked(uid)
    return True


def clear_user_tokens(uid: int) -> None:
    """清空用户托管与缓存(登出/刷新失败时调用)。"""
    storage = _storage()
    if storage is not None:
        try:
            storage.delete_sso_tokens(uid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"清理用户 {uid} SSO token 失败: {e}")
    with _lock:
        _forget_locked(uid)


def _fresh_id_token(storage, uid: int) -> tuple[str, str, str | None]:
    """取新鲜 id_token: 未到期直接用; 自家/web 行临近/已过期用 refresh_token 刷新并回存。

    单一写者: 桌面客户端(dashboard-gateway)负责刷新并复投其托管行, agent 对桌面行只读
    id_token(留 60s 余量): 有效直接返回, 已过期返回空串且**不调用 refresh_token_grant**、
    不删除托管行, 由调用方 fail-closed 引导登录(不回退服务身份)。

    返回 (id_token, client_id, client_secret); 取不到时 id_token 为空串。
    """
    row = storage.get_sso_tokens(uid)
    if not row:
        return "", "", None
    id_token = str(row.get("id_token") or "")
    refresh_token = str(row.get("refresh_token") or "")
    exp = float(row.get("id_expires_at") or 0)
    client_id, client_secret = _client_creds(row)
    if client_id != sso_auth.sso_client_id():
        # 桌面行: agent 只读 id_token, 绝不与桌面竞态消费单次使用的 refresh_token
        if id_token and exp - time.time() > _DESKTOP_READ_MARGIN_SECONDS:
            return id_token, client_id, client_secret
        return "", client_id, client_secret
    if id_token and exp - time.time() > _REFRESH_MARGIN_SECONDS:
        return id_token, client_id, client_secret
    if not refresh_token:
        return "", client_id, client_secret
    try:
        refreshed = sso_auth.refresh_token_grant(
            refresh_token, client_id=client_id, client_secret=client_secret
        )
    except sso_auth.SsoAuthError as e:
        logger.info(f"用户 {uid} SSO refresh 失败, 清空托管: {e}")
        storage.delete_sso_tokens(uid)
        return "", "", None
    new_id = str(refreshed.get("id_token") or "")
    if not new_id:
        return "", client_id, client_secret
    new_exp = float(sso_auth.token_exp(new_id) or 0)
    new_refresh = str(refreshed.get("refresh_token") or "") or refresh_token
    try:
        storage.save_sso_tokens(uid, new_id, new_refresh, new_exp, client_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"用户 {uid} 刷新后回存失败: {e}")
    return new_id, client_id, client_secret


def get_downstream_token(uid: int, audience: str = "", *, force: bool = False) -> str:
    """用户身份的下游 token; 无 SSO 会话/刷新或交换失败返回空串(fail-closed 由调用方引导登录)。

    force=True 时跳过缓存(下游返回 401 时重换一次)。
    """
    if uid <= 0:
        return ""
    aud = (audience or sso_auth.sso_downstream_audience() or "").strip()
    if not aud:
        return ""
    now = time.time()
    key = (uid, aud)
    with _lock:
        hit = _cache.get(key)
        if hit and not force and hit[1] - now > _EXCHANGE_MARGIN_SECONDS:
            return hit[0]
    storage = _storage()
    if storage is None:
        return ""
    # 低并发场景: 单锁串行刷新/交换, 避免 per-uid 锁复杂度
    with _lock:
        id_token, client_id, client_secret = _fresh_id_token(storage, uid)
        if not id_token:
            _forget_locked(uid)
            return ""
        try:
            token = sso_auth.exchange_token(
                id_token, aud, client_id=client_id, client_secret=client_secret
            )
        except sso_auth.SsoAuthError as e:
            logger.info(f"用户 {uid} SSO 交换({aud})失败: {e}")
            return ""
        exp = float(sso_auth.token_exp(token) or 0) or (time.time() + 3300.0)
        _cache[key] = (token, exp)
        return token


# 用户下游 token 不可用时的统一引导文案(市场/知识库工具与 web 代理 fail-closed 共用)
USER_TOKEN_HINT = "请先登录一次 AI 平台(https://ai.xzrobot.com)完成身份授权，后再使用市场/知识库能力。"


# 异常名与调用方约定固定为 UserTokenUnavailable(不带 Error 后缀), 豁免 N818
class UserTokenUnavailable(Exception):  # noqa: N818
    """用户下游 token 不可用(无 SSO 托管, 或刷新/交换失败); 调用方应 fail-closed。"""


def require_user_token(uid: int, audience: str = "") -> str:
    """取用户下游 token; 无托管/刷新失败 -> raise UserTokenUnavailable(固定引导文案)。

    audience 缺省取 ``sso_auth.sso_downstream_audience()``(与 get_downstream_token 一致)。
    """
    token = get_downstream_token(uid, audience)
    if not token:
        raise UserTokenUnavailable(USER_TOKEN_HINT)
    return token
