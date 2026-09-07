"""管理端可观测/用量/个人用量 Router（Wave B：从 web/server.py 拆出的域）。

端点语义与 server.py 迁移前完全一致；共享状态通过传入的 WebServer 实例访问。
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from web.security import get_auth, require_admin

logger = logging.getLogger("agent.web.admin")


def build_admin_router(server) -> APIRouter:
    router = APIRouter()

    def _uptime_s(started_at_iso: str) -> int:
        try:
            return int((datetime.now() - datetime.fromisoformat(started_at_iso)).total_seconds())
        except Exception:
            return 0

    @router.get("/api/admin/stats")
    async def admin_stats(request: Request):
        if require_admin(request) is None:
            return JSONResponse({"error": "Admin required"}, status_code=403)
        from storage.storage import get_storage
        storage = get_storage()
        live = server.live_sessions_snapshot()
        running = [s for s in live if s["is_streaming"]]

        agent_active_sessions, running_agents, task_counts = 0, 0, {}
        subagent_active = 0
        if server.agent:
            sm = getattr(server.agent, "session_manager", None)
            if sm:
                agent_active_sessions = sm.get_session_count()
            sm2 = getattr(server.agent, "subagent_manager", None)
            if sm2:
                try:
                    subagent_active = len(sm2._active_subagents)
                except Exception:
                    subagent_active = 0
            task_mgr = getattr(server.agent, "task_manager", None)
            try:
                tasks = task_mgr.list_tasks() if task_mgr else []
                task_counts = {"pending": 0, "running": 0, "completed": 0,
                               "failed": 0, "cancelled": 0}
                for t in tasks:
                    s = t.get("status", "pending")
                    if s in task_counts:
                        task_counts[s] += 1
                running_agents = task_counts.get("running", 0)
            except Exception:
                pass
        else:
            return JSONResponse({"error": "Agent not initialized"}, status_code=503)

        usage_today = {}
        online = {"count": 0, "users": []}
        if storage:
            try:
                usage_today = storage.usage_totals(days=1)
                rows = storage.query_usage(limit=200)
            except Exception:
                rows = []
            try:
                recent_uids: set[str] = set()
                for r in rows:
                    if r.get("user_id") and r["user_id"] != "system":
                        recent_uids.add(str(r["user_id"]))
                for s in running:
                    if s.get("tag"):
                        recent_uids.add(s["tag"])
                online["users"] = [
                    {"uid": t, "name": server._user_display_name(t)}
                    for t in recent_uids
                ]
                online["count"] = len(online["users"])
            except Exception:
                pass

        client = getattr(server.agent, "client", None)
        return {
            "now": datetime.now().isoformat(),
            "uptime_s": _uptime_s(getattr(server, "_started_at", "")),
            "agent": {
                "name": getattr(server.agent, "name", "") or "Agent",
                "status": getattr(server.agent, "status", ""),
                "model": getattr(client, "model", "") if client else "",
            },
            "concurrency": {
                "web_sessions": len(live),
                "running_streams": len(running),
                "agent_active_sessions": agent_active_sessions,
                "subagent_active": subagent_active,
                "running_tasks": running_agents,
                "task_counts": task_counts,
            },
            "pool": server._pool.stats() if (getattr(server, "_pool", None) is not None
                                             and server._pool.enabled)
                    else {"enabled": False, "capacity": 0, "overflow": 0, "hard_cap": 0,
                          "active": 0, "busy": 0, "total_created": 0, "users": []},
            "usage_today": usage_today,
            "perf_today": {k: v for k, v in (usage_today or {}).items()
                           if "duration_ms" in k or k == "calls"},
            "online": online,
            "live_sessions": live,
        }

    @router.get("/api/admin/usage")
    async def admin_usage(days: int = Query(7), group: str = Query("day"),
                          model: str = Query(""), user: str = Query(""),
                          request: Request = None):
        if require_admin(request) is None:
            return JSONResponse({"error": "Admin required"}, status_code=403)
        from storage.storage import get_storage
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        days = max(1, min(days, 90))
        if group not in ("day", "user", "model", "session"):
            group = "day"
        totals = storage.usage_totals(days=days, user_id=user, model=model)
        breakdown = storage.summarize_usage(group_by=group, days=days,
                                            user_id=user, model=model)
        recent = storage.query_usage(user_id=user, limit=100)
        return {"days": days, "group": group, "totals": totals,
                "breakdown": breakdown, "recent": recent}

    @router.get("/api/usage")
    async def my_usage(days: int = Query(7), request: Request = None):
        """个人用量（每个登录用户查看自己的 token / 成本 / 性能）。"""
        u = get_auth(request)
        raw_uid = str(u.get("uid", ""))
        if not raw_uid or raw_uid in ("anon", "0", "None"):
            return {"enabled": False, "reason": "no_identity"}
        tag = WebServerOwner.tag(raw_uid)
        from storage.storage import get_storage
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        days = max(1, min(days, 90))
        try:
            totals = storage.usage_totals(days=days, user_id=tag)
            by_day = storage.summarize_usage("day", days=days, user_id=tag)
            by_model = storage.summarize_usage("model", days=days, user_id=tag)
            recent = storage.query_usage(user_id=tag, limit=30)
        except Exception as e:
            logger.warning(f"[usage/mine] 查询失败: {e}")
            totals, by_day, by_model, recent = {}, [], [], []
        return {"user": u.get("name", raw_uid), "tag": tag, "days": days,
                "totals": totals, "by_day": by_day, "by_model": by_model, "recent": recent}

    @router.get("/api/admin/sessions")
    async def admin_sessions(limit: int = Query(30), scope: str = Query("all"),
                             request: Request = None):
        if require_admin(request) is None:
            return JSONResponse({"error": "Admin required"}, status_code=403)
        from storage.storage import get_storage
        storage = get_storage()
        live = server.live_sessions_snapshot() if scope in ("all", "live") else []
        history: list[dict] = []
        if storage and scope in ("all", "history"):
            rows = storage.list_conversations(min(max(limit, 1), 500))
            for r in rows:
                tag = r.get("user_id") or ""
                history.append({
                    "id": r["conversation_id"],
                    "agent_id": r.get("agent_id") or "",
                    "owner": server._user_display_name(tag) if tag else "",
                    "uid": tag[4:] if tag.startswith("web:") else tag,
                    "tag": tag,
                    "messages": r["msg_count"],
                    "thread_count": r.get("thread_count") or 0,
                    "last_accessed": r["last_at"],
                    "first_accessed": r["first_at"],
                })
        return {"live": live, "history": history}

    @router.get("/api/admin/sessions/running")
    async def admin_sessions_running(request: Request = None):
        """管理端「运行中」会话：全部正在执行、占用 Agent worker 的会话（含 user 姓名）。

        判定口径与 /api/agent/sessions/running 一致：worker 池启用时以池登记为准，
        关闭时即内存流式中会话（metrics agent_running_streams 同源）。admin 全量可见。
        """
        if require_admin(request) is None:
            return JSONResponse({"error": "Admin required"}, status_code=403)
        sessions = server.running_sessions_snapshot(admin=True, tag="")
        return {"total": len(sessions), "sessions": sessions}

    @router.get("/api/admin/sessions/{session_id}/threads")
    async def admin_session_threads(session_id: str, request: Request = None):
        if require_admin(request) is None:
            return JSONResponse({"error": "Admin required"}, status_code=403)
        from storage.storage import get_storage
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        threads = storage.conversation_threads(session_id)
        return {"conversation_id": session_id, "threads": threads, "count": len(threads)}

    @router.get("/api/admin/sessions/{session_id}/messages")
    async def admin_session_messages(session_id: str, request: Request = None):
        if require_admin(request) is None:
            return JSONResponse({"error": "Admin required"}, status_code=403)
        from storage.storage import get_storage
        storage = get_storage()
        if storage:
            msgs = storage.get_messages_with_meta(session_id, limit=0)
            if msgs:
                return {"session_id": session_id, "source": "db", "messages": msgs,
                        "count": len(msgs)}
        with server._session_lock:
            cs = server._sessions.get(session_id)
        if cs:
            return {"session_id": session_id, "source": "memory", "messages": cs.snapshot(),
                    "count": len(cs.snapshot())}
        return JSONResponse({"error": "Session not found"}, status_code=404)

    @router.post("/api/admin/sessions/{session_id}/messages")
    async def admin_session_export(session_id: str, request: Request = None):
        """审计导出：返回全部消息的 JSON（含元数据），由管理端保存为审计文件。"""
        if require_admin(request) is None:
            return JSONResponse({"error": "Admin required"}, status_code=403)
        from storage.storage import get_storage
        storage = get_storage()
        if storage:
            msgs = storage.get_messages_with_meta(session_id)
            if msgs:
                return {"session_id": session_id,
                        "exported_at": datetime.now().isoformat(),
                        "messages": msgs, "count": len(msgs)}
        return JSONResponse({"error": "Session not found"}, status_code=404)

    return router


class WebServerOwner:
    """个人用量归属 tag 辅助（与 WebServer._owner_tag 一致），避免在 router 引入 server 强依赖。"""

    @staticmethod
    def tag(uid) -> str:
        return f"web:{uid}"
