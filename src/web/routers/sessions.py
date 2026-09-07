"""会话(Web 内存对话 + 归属校验) Router —— Wave B：从 web/server.py 拆出的域。

端点: GET /api/sessions · GET/DELETE /api/sessions/{id}(messages)；
语义与迁移前一致，owner/admin 校验经 WebServer 静态/实例辅助完成。
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from web.security import get_auth

logger = logging.getLogger("agent.web.sessions")


def build_sessions_router(server) -> APIRouter:
    router = APIRouter()

    def _identity(request: Request):
        """返回 (admin, tag)；鉴权缺失场景退化为 admin(与 DISABLE_AUTH 一致)。"""
        try:
            u = get_auth(request)
            return u.get("role") == "admin", server._owner_tag(str(u.get("uid")))
        except Exception:
            return True, ""

    @router.get("/api/sessions")
    async def list_sessions(request: Request, scope: str = Query("mine")):
        """内存 Web 会话列表（对话侧栏 / 运维会话管理共用）。

        scope=mine（默认）：无论角色只看本人（个人空间「对话」侧栏，admin 亦只看自己）；
        仅当 admin 显式传 scope=all 时返回全站（运维「会话管理」合并展示）。
        """
        admin, tag = _identity(request)
        want_all = admin and scope == "all"
        now = datetime.now()
        out = []
        with server._session_lock:
            items = list(server._sessions.items())
        for sid, s in items:
            owner_tag = server._session_owners.get(sid, "")
            if not want_all and not server._same_owner(owner_tag, tag):
                continue
            duration_s = 0
            if s.is_streaming and s.stream_started_at:
                try:
                    duration_s = int((now - datetime.fromisoformat(s.stream_started_at)).total_seconds())
                except Exception:
                    duration_s = 0
            item = {
                "id": sid, "created_at": s.created_at,
                "message_count": s.message_count(),
                "is_streaming": s.is_streaming,
                "duration_s": duration_s,
            }
            if admin:
                info = server._owner_display(sid, owner_tag or tag)
                item.update({"user_id": info["uid"], "owner": info["name"], "tag": info["tag"]})
            out.append(item)
        out.sort(key=lambda x: x["created_at"], reverse=True)
        return {"sessions": out}

    @router.get("/api/sessions/{session_id}/messages")
    async def session_messages(session_id: str, request: Request):
        admin, tag = _identity(request)
        if not admin and server._session_access(session_id, tag, admin) == "deny":
            return JSONResponse({"error": "Session not found"}, status_code=404)
        with server._session_lock:
            chat_session = server._sessions.get(session_id)
        if not chat_session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return {"messages": chat_session.snapshot()}

    @router.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        admin, tag = _identity(request)
        if not admin and server._session_access(session_id, tag, admin) == "deny":
            return JSONResponse({"error": "Session not found"}, status_code=404)
        with server._session_lock:
            server._sessions.pop(session_id, None)
            server._session_owners.pop(session_id, None)
            server._session_owner_names.pop(session_id, None)
        return {"success": True}

    return router
