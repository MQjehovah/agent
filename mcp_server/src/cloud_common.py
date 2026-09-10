# -*- coding: utf-8 -*-
"""
rosiwit-cloud 共享鉴权与 HTTP 封装。

供 ticket_ops 与 remote_operation 复用。

当前写死国内云 https://bms-cn.xzrobot.com，不做多云探测。

登录策略：
- 自动登录一次后把 token + 过期时间写入本机共享文件
- ticket_ops / remote_operation / 进程重启在过期前复用，不重复登录
- 仅过期、即将过期、或接口明确 token 非法时才重新登录
"""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Optional

import requests

logger = logging.getLogger("cloud-common")

# 工单/设备 API 写死国内云，不做多云探测（避免登录限流）。
DEFAULT_CLOUD_BASE = "https://bms-cn.xzrobot.com"
API_BASE_URL = DEFAULT_CLOUD_BASE
LOGIN_PATH = os.getenv("TICKET_LOGIN_PATH", "/rosiwit-cloud/auth/login")
TICKET_DETAIL_PATH_TMPL = os.getenv(
    "TICKET_DETAIL_PATH",
    "/rosiwit-cloud/ticket/ops/detail/{ticket_id}",
)
USERNAME = os.getenv("TICKET_API_USERNAME") or os.getenv("DEVICE_API_USERNAME", "")
PASSWORD = os.getenv("TICKET_API_PASSWORD") or os.getenv("DEVICE_API_PASSWORD", "")
CLIENT_TYPE = os.getenv("TICKET_CLIENT_TYPE", "WEB")
TIMEOUT = int(os.getenv("TICKET_API_TIMEOUT", "30"))
LOGIN_COOLDOWN_SECONDS = int(os.getenv("TICKET_LOGIN_COOLDOWN_SECONDS", "900"))
# 登录响应未给过期时间时的兜底 TTL（秒）。过期前不会主动再登录。
TOKEN_TTL_SECONDS = int(os.getenv("TICKET_TOKEN_TTL_SECONDS", str(12 * 3600)))
TOKEN_REFRESH_SKEW_SECONDS = int(os.getenv("TICKET_TOKEN_REFRESH_SKEW_SECONDS", "60"))


def _default_runtime_dir() -> str:
    """跨进程 token 目录：Windows 用 LocalAppData，其它用 ~/.cache。勿默认 /tmp。"""
    custom = (os.getenv("CLOUD_STATE_DIR") or os.getenv("AGENT_RUNTIME_DIR") or "").strip()
    if custom:
        return custom
    if os.name == "nt":
        root = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or tempfile.gettempdir()
        return os.path.join(root, "xzrobot-agent")
    xdg = os.getenv("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(xdg, "xzrobot-agent")


def _default_state_path(env_key: str, filename: str) -> str:
    override = (os.getenv(env_key) or "").strip()
    if override:
        return override
    return os.path.join(_default_runtime_dir(), filename)


# ticket_ops / remote_operation 分进程，用文件同步当前云与 token
BIND_STATE_PATH = _default_state_path("CLOUD_BIND_STATE_PATH", "cloud_bind.json")
TOKEN_STATE_PATH = _default_state_path("CLOUD_TOKEN_STATE_PATH", "cloud_tokens.json")

_lock = threading.RLock()
_tls = threading.local()
_active_base: str = API_BASE_URL.rstrip("/")
_token_by_base: dict[str, Optional[str]] = {}
_token_expires_at: dict[str, float] = {}
_ticket_base_cache: dict[str, str] = {}
_login_cooldown_until: dict[str, float] = {}

_seed = os.getenv("TICKET_API_TOKEN") or os.getenv("CLOUD_API_TOKEN")
if _seed:
    _token_by_base[_active_base] = _seed.strip() or None
    _token_expires_at[_active_base] = 0.0  # 手工注入：本地不过期，等接口 401 再刷


def configured_base_urls() -> list[str]:
    """当前写死国内云，忽略 TICKET_API_BASE_URLS。"""
    return [DEFAULT_CLOUD_BASE]


def _credentials() -> tuple[str, str]:
    user = os.getenv("TICKET_API_USERNAME") or os.getenv("DEVICE_API_USERNAME") or USERNAME
    pwd = os.getenv("TICKET_API_PASSWORD") or os.getenv("DEVICE_API_PASSWORD") or PASSWORD
    return (user or "").strip(), (pwd or "").strip()


def _read_json_file(path: str) -> dict[str, Any]:
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("读取 JSON 失败 path=%s: %s", path, e)
        return {}


def _write_json_file(path: str, payload: dict[str, Any]) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("写入 JSON 失败 path=%s: %s", path, e)
        try:
            os.remove(f"{path}.tmp.{os.getpid()}")
        except OSError:
            pass


@contextmanager
def _interprocess_lock():
    """登录/写 token 的跨进程锁，避免 ticket_ops 与 remote_operation 同时登录。"""
    depth = getattr(_tls, "lock_depth", 0)
    if depth:
        _tls.lock_depth = depth + 1
        try:
            yield
        finally:
            _tls.lock_depth = depth
        return

    lock_path = f"{TOKEN_STATE_PATH}.lock"
    parent = os.path.dirname(lock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fh = open(lock_path, "a+b")
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                if fh.read(1) == b"":
                    fh.write(b"0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        except Exception as e:
            logger.debug("token 文件锁不可用，回退进程内锁: %s", e)
            with _lock:
                yield
            return
        _tls.lock_depth = 1
        try:
            yield
        finally:
            _tls.lock_depth = 0
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        fh.close()


def _as_epoch(value: Any) -> Optional[float]:
    """把过期字段收成 epoch 秒：绝对时间 / 毫秒 / 剩余秒数 / 日期字符串。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n <= 0:
            return None
        if n > 1e12:
            return n / 1000.0
        if n > 1e9:
            return n
        if 60 <= n <= 366 * 24 * 3600:
            return time.time() + n
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.replace(".", "", 1).isdigit():
            return _as_epoch(float(text))
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y/%m/%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(text[:19].replace("Z", ""), fmt.replace("Z", "")).timestamp()
            except ValueError:
                continue
    return None


def _jwt_exp(token: str) -> Optional[float]:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    pad = "=" * ((4 - len(payload) % 4) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + pad).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return _as_epoch(data.get("exp"))


def extract_expires_at(result: dict[str, Any], token: Optional[str] = None) -> float:
    """从登录响应 / JWT 解析过期时间；都没有则用默认 TTL。"""
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    token_info = data.get("tokenInfo") if isinstance(data.get("tokenInfo"), dict) else {}
    keys = (
        "expireTime",
        "expireAt",
        "expiresAt",
        "expire",
        "expires",
        "expireIn",
        "expiresIn",
        "expireSeconds",
        "tokenExpireTime",
        "validTime",
        "ttl",
    )
    for src in (token_info, data, result):
        if not isinstance(src, dict):
            continue
        for key in keys:
            epoch = _as_epoch(src.get(key))
            if epoch:
                return epoch
    jwt_exp = _jwt_exp(token or "")
    if jwt_exp:
        return jwt_exp
    return time.time() + max(60, TOKEN_TTL_SECONDS)


def _parse_token_entry(value: Any, fallback_updated_at: float = 0.0) -> tuple[str, float]:
    if isinstance(value, str):
        token = value.strip()
        exp = 0.0
        if token and fallback_updated_at > 0 and TOKEN_TTL_SECONDS > 0:
            exp = fallback_updated_at + TOKEN_TTL_SECONDS
        return token, exp
    if isinstance(value, dict):
        token = str(value.get("token") or "").strip()
        exp = _as_epoch(value.get("expiresAt") or value.get("expireAt")) or 0.0
        return token, exp
    return "", 0.0


def _token_if_valid(base: str) -> Optional[str]:
    token = _token_by_base.get(base)
    if not token:
        return None
    exp = float(_token_expires_at.get(base) or 0)
    if exp > 0 and time.time() >= exp - TOKEN_REFRESH_SKEW_SECONDS:
        return None
    return token


def _load_token_state() -> None:
    """从共享文件加载各云 token（跨 MCP 进程复用，过期前一直有效）。"""
    data = _read_json_file(TOKEN_STATE_PATH)
    tokens = data.get("tokens")
    updated_at = _as_epoch(data.get("updatedAt")) or 0.0
    with _lock:
        if isinstance(tokens, dict):
            for base, raw in tokens.items():
                key = str(base).strip().rstrip("/")
                token, exp = _parse_token_entry(raw, fallback_updated_at=updated_at)
                if not key or not token:
                    continue
                _token_by_base[key] = token
                _token_expires_at[key] = exp
        cooldowns = data.get("cooldownUntil")
        if isinstance(cooldowns, dict):
            now = time.time()
            for base, until in cooldowns.items():
                key = str(base).strip().rstrip("/")
                epoch = _as_epoch(until) or 0.0
                if key and epoch > now:
                    current = _login_cooldown_until.get(key, 0)
                    if epoch > current:
                        _login_cooldown_until[key] = epoch


def _persist_token_state() -> None:
    """将内存中的 token / 冷却写入共享文件。"""
    now = time.time()
    with _lock:
        tokens: dict[str, Any] = {}
        for base, token in _token_by_base.items():
            if not base or not token:
                continue
            tokens[base] = {
                "token": token,
                "expiresAt": float(_token_expires_at.get(base) or 0),
            }
        cooldowns = {
            base: until
            for base, until in _login_cooldown_until.items()
            if base and until > now
        }
    _write_json_file(
        TOKEN_STATE_PATH,
        {"tokens": tokens, "cooldownUntil": cooldowns, "updatedAt": int(now)},
    )


def _load_ticket_bases_from_bind() -> None:
    """从绑定状态文件加载 ticket_id → baseUrl 映射。"""
    data = _read_json_file(BIND_STATE_PATH)
    ticket_bases = data.get("ticketBases")
    if not isinstance(ticket_bases, dict):
        return
    with _lock:
        for tid, base in ticket_bases.items():
            key = str(tid).strip()
            url = str(base).strip().rstrip("/")
            if key and url:
                _ticket_base_cache[key] = url


def _write_bind_state(ticket_id: str | None = None) -> None:
    existing = _read_json_file(BIND_STATE_PATH)
    ticket_bases = existing.get("ticketBases")
    if not isinstance(ticket_bases, dict):
        ticket_bases = {}
    tid = (ticket_id or "").strip() or None
    base = _active_base.rstrip("/")
    if tid and base:
        ticket_bases[tid] = base
    payload = {
        "baseUrl": base,
        "ticketId": tid,
        "ticketBases": ticket_bases,
    }
    _write_json_file(BIND_STATE_PATH, payload)


def _load_bind_state() -> None:
    """从共享文件刷新 _active_base 与 ticket 缓存。"""
    global _active_base
    data = _read_json_file(BIND_STATE_PATH)
    base = str((data or {}).get("baseUrl") or "").strip().rstrip("/")
    if base and base != _active_base:
        _active_base = base
        logger.info("从绑定状态加载云 Base URL: %s", base)
    _load_ticket_bases_from_bind()


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
    _load_token_state()
    with _lock:
        return _token_if_valid(get_base_url())


def set_token(token: Optional[str], expires_at: Optional[float] = None) -> None:
    value = (token or "").strip() or None
    exp = float(expires_at or 0)
    if value and exp <= 0:
        jwt_exp = _jwt_exp(value)
        exp = jwt_exp or (time.time() + max(60, TOKEN_TTL_SECONDS))
    with _interprocess_lock():
        with _lock:
            base = get_base_url()
            _token_by_base[base] = value
            if value:
                _token_expires_at[base] = exp
            else:
                _token_expires_at.pop(base, None)
        _persist_token_state()


def _login_in_cooldown(base: str) -> bool:
    _load_token_state()
    until = _login_cooldown_until.get(base.rstrip("/"), 0)
    return until > time.time()


def _set_login_cooldown(base: str, seconds: int | None = None) -> None:
    cooldown = seconds if seconds is not None else LOGIN_COOLDOWN_SECONDS
    _login_cooldown_until[base.rstrip("/")] = time.time() + max(1, cooldown)
    logger.warning("登录冷却 %ds: base=%s", cooldown, base)
    _persist_token_state()


def _is_rate_limited_login(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    code = result.get("returnCode")
    msg = str(result.get("returnMsg") or result.get("error") or result.get("message") or "")
    if code in (3001010, "3001010"):
        return True
    return "过于频繁" in msg or "稍后再" in msg


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
    """使用 rosiwit-cloud 认证中心登录（form-urlencoded，字段名 userName）。

    默认复用未过期的全局 token；force=True 仅用于接口判定 token 已非法。
    """
    base = get_base_url().rstrip("/")
    user, password = _credentials()
    with _interprocess_lock():
        _load_token_state()
        if not force:
            existing = _token_if_valid(base)
            if existing:
                return
        if _login_in_cooldown(base):
            logger.warning("跳过登录（冷却中）: base=%s", base)
            return
        if not user or not password:
            logger.warning("CLOUD/TICKET/DEVICE API 账号未配置，跳过自动登录")
            return
        url = f"{base}{LOGIN_PATH}"
        try:
            resp = requests.post(
                url,
                data={
                    "userName": user,
                    "password": password,
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
            if token and isinstance(result, dict) and (
                result.get("success") or result.get("returnCode") == 200
            ):
                expires_at = extract_expires_at(result, token)
                with _lock:
                    _token_by_base[base] = token
                    _token_expires_at[base] = expires_at
                _persist_token_state()
                logger.info(
                    "rosiwit-cloud auth 登录成功 (base=%s user=%s expires_in=%.0fs)",
                    base,
                    user,
                    max(0, expires_at - time.time()),
                )
            else:
                with _lock:
                    _token_by_base[base] = None
                    _token_expires_at.pop(base, None)
                if isinstance(result, dict) and _is_rate_limited_login(result):
                    _set_login_cooldown(base)
                else:
                    _persist_token_state()
                logger.warning(
                    "rosiwit-cloud 登录失败 base=%s: %s",
                    base,
                    (result or {}).get("returnMsg", "未知错误")
                    if isinstance(result, dict)
                    else result,
                )
        except Exception as e:
            with _lock:
                _token_by_base[base] = None
                _token_expires_at.pop(base, None)
            _persist_token_state()
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
    """有未过期全局 token 则直接用；否则登录一次并写入共享缓存。"""
    if get_token():
        return
    auto_login(force=False)


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
    used_token = get_token()
    if used_token:
        headers["token"] = used_token
    kwargs["headers"] = headers

    resp = requests.request(method, url, **kwargs)
    user, password = _credentials()
    if _is_token_invalid_response(resp) and user and password:
        if _login_in_cooldown(get_base_url()):
            return resp
        logger.info("检测到 token 失效，强制重新登录后重试: %s %s", method, url)
        with _interprocess_lock():
            _load_token_state()
            current = _token_if_valid(get_base_url())
            if not current or current == used_token:
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


def _probe_order(urls: list[str]) -> list[str]:
    """探测顺序：国内云默认优先，其次最近绑定的云。"""
    ordered: list[str] = []
    for preferred in (DEFAULT_CLOUD_BASE, get_base_url().rstrip("/")):
        if preferred in urls and preferred not in ordered:
            ordered.append(preferred)
    for url in urls:
        if url not in ordered:
            ordered.append(url)
    return ordered


def resolve_base_for_ticket(ticket_id: str) -> Optional[str]:
    """按 ticket_id 探测并绑定所在云；结果缓存。单云时直接绑定默认 URL。"""
    tid = str(ticket_id or "").strip()
    if not tid:
        return None

    _load_ticket_bases_from_bind()
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
        _write_bind_state(tid)
        return urls[0]

    last_error: Optional[str] = None
    for base in _probe_order(urls):
        if _login_in_cooldown(base):
            logger.info("跳过探测（登录冷却中）: ticket=%s base=%s", tid, base)
            last_error = f"登录冷却中: {base}"
            continue
        set_active_base(base, ticket_id=tid)
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
        cooled = [
            b for b in configured_base_urls()
            if _login_in_cooldown(b)
        ]
        hint = "检查 TICKET_API_BASE_URLS / 账号，或确认 ticket_id 正确"
        if cooled:
            hint = (
                f"部分云环境登录冷却中（{', '.join(cooled)}），"
                "请稍后重试或缩小 TICKET_API_BASE_URLS"
            )
        return {
            "success": False,
            "error": f"未能在已配置云环境中找到工单 {tid}",
            "tried": configured_base_urls(),
            "loginCooldown": cooled or None,
            "hint": hint,
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


# 启动时加载跨进程 token / 工单云映射，不主动登录（按需登录，命中未过期缓存则跳过）
_load_token_state()
_load_ticket_bases_from_bind()
