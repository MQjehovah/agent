import asyncio
import contextlib
import json
import logging
import os
import secrets
import threading

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from storage.storage import get_storage

logger = logging.getLogger("agent.web")

_jwt_secret: str = ""


def _load_jwt_secret() -> str:
    global _jwt_secret
    if _jwt_secret:
        return _jwt_secret
    s = os.environ.get("JWT_SECRET", "")
    if s:
        _jwt_secret = s
        return s
    f = os.path.join(os.path.dirname(__file__), "..", "..", "config", "jwt_secret")
    try:
        with open(f, encoding="utf-8") as fh:
            _jwt_secret = fh.read().strip()
        if _jwt_secret:
            return _jwt_secret
    except FileNotFoundError:
        pass
    _jwt_secret = secrets.token_hex(32)
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(_jwt_secret)
    return _jwt_secret


def create_jwt(user: dict, expires_seconds: int = 86400 * 7) -> str:
    import time
    payload = {"uid": user["id"], "name": user["name"], "role": user["role"],
               "exp": int(time.time()) + expires_seconds}
    return jwt.encode(payload, _load_jwt_secret(), algorithm="HS256")


def decode_jwt(token: str) -> dict:
    return jwt.decode(token, _load_jwt_secret(), algorithms=["HS256"])


def _sse(payload: dict) -> str:
    """格式化一条 SSE 事件"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class LogStreamHandler(logging.Handler):
    """把日志记录广播给所有 SSE 订阅者"""

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        super().__init__()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop = loop
        self._lock = threading.Lock()

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        with self._lock:
            self._subscribers.discard(q)

    def emit(self, record: logging.LogRecord):
        try:
            line = self.format(record)
            loop = self._loop
            with self._lock:
                subs = list(self._subscribers)
            if not loop or not subs:
                return
            for q in subs:
                loop.call_soon_threadsafe(self._safe_put, q, line)
        except Exception:
            pass

    def _safe_put(self, q: asyncio.Queue, line: str):
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(line)


async def get_auth(request: Request) -> dict:
    """从请求中提取认证信息"""
    if os.environ.get("WEBUI_DISABLE_AUTH") == "1":
        return {"uid": 1, "name": "test", "role": "admin"}
    h = request.headers.get("Authorization", "")
    return decode_jwt(h[7:])


async def get_admin(request: Request) -> dict:
    """获取认证信息并校验 admin 角色"""
    u = await get_auth(request)
    if u.get("role") != "admin":
        raise HTTPException(403, "Admin required")
    return u


def register_auth_routes(app, ws):
    """注册认证相关路由（auth middleware + auth endpoints）"""

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        if os.environ.get("WEBUI_DISABLE_AUTH") == "1":
            return await call_next(request)
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/auth/"):
            h = request.headers.get("Authorization", "")
            if not h.startswith("Bearer "):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                decode_jwt(h[7:])
            except Exception:
                return JSONResponse({"error": "Invalid or expired token"}, status_code=401)
        return await call_next(request)

    @app.post("/api/auth/login")
    async def auth_login(request: Request):
        data = await request.json()
        username = (data.get("username") or "").strip()
        password = data.get("password", "")
        if not username or not password:
            return JSONResponse({"error": "Missing credentials"}, status_code=400)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=500)
        user = storage.verify_user_password(username, password)
        if not user:
            return JSONResponse({"error": "Invalid username or password"}, status_code=401)
        token = create_jwt(user)
        return {"token": token, "user": {"id": user["id"], "name": user["name"],
                 "role": user["role"], "department": user.get("department", "")}}

    @app.get("/api/auth/me")
    async def auth_me(request: Request):
        u = await get_auth(request)
        return {"id": u["uid"], "name": u["name"], "role": u["role"]}

    @app.post("/api/auth/change-password")
    async def auth_change_password(request: Request):
        u = await get_auth(request)
        data = await request.json()
        old_pw = data.get("old_password", "")
        new_pw = (data.get("new_password") or "").strip()
        if not new_pw or len(new_pw) < 4:
            return JSONResponse({"error": "New password too short (min 4)"}, status_code=400)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=500)
        with storage.get_connection() as conn:
            row = conn.execute(
                "SELECT password_hash FROM rbac_users WHERE id = ?", (u["uid"],)
            ).fetchone()
        if row:
            import base64
            import hashlib
            try:
                raw = base64.b64decode(row["password_hash"])
                salt, stored = raw[:16], raw[16:]
                if hashlib.pbkdf2_hmac("sha256", old_pw.encode(), salt, 200000) != stored:
                    return JSONResponse({"error": "Old password incorrect"}, status_code=403)
            except Exception:
                return JSONResponse({"error": "Old password incorrect"}, status_code=403)
        storage.set_user_password(u["uid"], new_pw)
        return {"success": True}

    @app.post("/api/auth/set-password")
    async def auth_set_password(request: Request):
        """Admin 为用户设置/重置密码"""
        await get_admin(request)
        data = await request.json()
        uid = data.get("user_id")
        new_pw = (data.get("password") or "").strip()
        if not uid or len(new_pw) < 4:
            return JSONResponse({"error": "Invalid params"}, status_code=400)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=500)
        storage.set_user_password(uid, new_pw)
        return {"success": True}
