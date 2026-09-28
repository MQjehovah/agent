"""按用户的 SSO token 托管与下游代换(web OBO, RFC 8693)。

登录回调把用户 id_token(约 10 分钟)/refresh_token 存入本地存储, 本模块负责:

- id_token 临近过期时用 refresh_token 刷新(轮换后回存), 刷新失败清空托管;
- 按受众交换下游 token(默认 1 小时), 进程内按 (uid, audience) 缓存;
- 用户未走 SSO(如本地密码登录)或托管缺失时返回空串, 由调用方回退服务身份。

受众默认 ``dashboard-gateway``(rag/market 资源轨均接受), 可用
``SSO_DOWNSTREAM_AUDIENCE`` / config.json ``sso.downstream_audience`` 覆盖。
"""

import logging
import threading
import time

from web import sso_auth

logger = logging.getLogger("agent.web.sso_tokens")

_REFRESH_MARGIN_SECONDS = 120.0  # id_token 剩余寿命低于该值即刷新
_EXCHANGE_MARGIN_SECONDS = 60.0  # 下游 token 剩余寿命低于该值视为过期

_lock = threading.Lock()
_cache: dict[tuple[int, str], tuple[str, float]] = {}


def _storage():
    from storage.storage import get_storage

    return get_storage()


def _forget_locked(uid: int) -> None:
    for key in [k for k in _cache if k[0] == uid]:
        _cache.pop(key, None)


def save_user_tokens(uid: int, tokens: dict) -> bool:
    """SSO 回调: 保存 id_token/refresh_token(幂等覆盖), 失败返回 False(不阻断登录)。"""
    if uid <= 0 or not isinstance(tokens, dict):
        return False
    id_token = str(tokens.get("id_token") or "")
    if not id_token:
        return False
    refresh_token = str(tokens.get("refresh_token") or "")
    exp = float(sso_auth.token_exp(id_token) or 0)
    storage = _storage()
    if storage is None:
        return False
    try:
        storage.save_sso_tokens(uid, id_token, refresh_token, exp)
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


def _fresh_id_token(storage, uid: int) -> str:
    """取新鲜 id_token: 未到期直接用; 临近/已过期用 refresh_token 刷新并回存。"""
    row = storage.get_sso_tokens(uid)
    if not row:
        return ""
    id_token = str(row.get("id_token") or "")
    refresh_token = str(row.get("refresh_token") or "")
    exp = float(row.get("id_expires_at") or 0)
    if id_token and exp - time.time() > _REFRESH_MARGIN_SECONDS:
        return id_token
    if not refresh_token:
        return ""
    try:
        refreshed = sso_auth.refresh_token_grant(refresh_token)
    except sso_auth.SsoAuthError as e:
        logger.info(f"用户 {uid} SSO refresh 失败, 清空托管: {e}")
        storage.delete_sso_tokens(uid)
        return ""
    new_id = str(refreshed.get("id_token") or "")
    if not new_id:
        return ""
    new_exp = float(sso_auth.token_exp(new_id) or 0)
    new_refresh = str(refreshed.get("refresh_token") or "") or refresh_token
    try:
        storage.save_sso_tokens(uid, new_id, new_refresh, new_exp)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"用户 {uid} 刷新后回存失败: {e}")
    return new_id


def get_downstream_token(uid: int, audience: str = "", *, force: bool = False) -> str:
    """用户身份的下游 token; 无 SSO 会话/刷新或交换失败返回空串(调用方回退服务身份)。

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
        id_token = _fresh_id_token(storage, uid)
        if not id_token:
            _forget_locked(uid)
            return ""
        try:
            token = sso_auth.exchange_token(id_token, aud)
        except sso_auth.SsoAuthError as e:
            logger.info(f"用户 {uid} SSO 交换({aud})失败: {e}")
            return ""
        exp = float(sso_auth.token_exp(token) or 0) or (time.time() + 3300.0)
        _cache[key] = (token, exp)
        return token
