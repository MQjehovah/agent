import asyncio
import logging
import os
import uuid
from datetime import datetime

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from storage.storage import get_storage

logger = logging.getLogger("agent.web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def register_api_routes(app, ws):
    """注册所有业务 API 路由"""

    from channels import MessageRouter
    from web.auth import _sse, decode_jwt, get_admin

    # ===== 静态资源 =====
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    _index_cache: dict = {}

    @app.get("/")
    async def index():
        idx = os.path.join(STATIC_DIR, "index.html")
        if not os.path.isfile(idx):
            raise HTTPException(404, "index.html not found")
        key = int(os.path.getmtime(idx))
        cached = _index_cache.get(key)
        if cached is not None:
            return HTMLResponse(cached)
        with open(idx, encoding="utf-8") as f:
            html = f.read()
        for name in ("app.js", "style.css"):
            p = os.path.join(STATIC_DIR, name)
            if os.path.isfile(p):
                mt = int(os.path.getmtime(p))
                html = html.replace(f"/static/{name}", f"/static/{name}?v={mt}")
        _index_cache.clear()
        _index_cache[key] = html
        return HTMLResponse(html)

    # ===== Agent Status =====
    @app.get("/api/agent/status")
    async def agent_status():
        if not ws.agent:
            return JSONResponse({"error": "Agent not initialized"}, status_code=503)
        task_mgr = ws.agent.task_manager
        tasks = task_mgr.list_tasks() if task_mgr else []
        status_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
        for t in tasks:
            s = t.get("status", "pending")
            if s in status_counts:
                status_counts[s] += 1
        usage = ws.agent.client.usage_tracker.get_summary() if ws.agent.client else {}

        subagents = []
        if ws.agent.subagent_manager:
            for name in ws.agent.subagent_manager.list_templates():
                tmpl = ws.agent.subagent_manager.get_template(name)
                desc = (tmpl.get("description", "") or "")[:80] if tmpl else ""
                subagents.append({"name": name, "description": desc})

        panel_stats = {}
        if ws._kanban:
            panel_stats = ws._kanban.get_stats()

        return {
            "name": ws.agent.name or "Agent",
            "description": ws.agent.description or "",
            "status": ws.agent.status,
            "model": ws.agent.client.model if ws.agent.client else "",
            "tasks": status_counts,
            "usage": usage,
            "tools": ws.agent.tool_registry.list_tools() if ws.agent.tool_registry else [],
            "subagents": subagents,
            "panel": panel_stats,
        }

    # ===== Chat =====
    @app.post("/api/chat")
    async def chat(request: Request):
        if not ws.agent:
            return JSONResponse({"error": "Agent not initialized"}, status_code=503)
        try:
            auth = await decode_jwt(request.headers.get("Authorization", "")[7:]) if os.environ.get("WEBUI_DISABLE_AUTH") != "1" else {"uid": 1, "name": "test", "role": "admin"}
        except Exception:
            auth = {"uid": "anon", "name": "匿名用户"}
        data = await request.json()
        if not data or not data.get("message"):
            return JSONResponse({"error": "Missing message"}, status_code=400)
        message = data["message"].strip()
        if not message:
            return JSONResponse({"error": "Empty message"}, status_code=400)

        router = MessageRouter(ws.agent)
        session_id = data.get("session_id") or router.format_session_id("web", uuid.uuid4().hex[:8])
        chat_session = ws._get_or_create_session(session_id)
        chat_session.add_message("user", message)
        chat_session.is_streaming = True

        web_user_id = f"web:{auth['uid']}"

        async def _web_auto_run():
            await router.publish(message, channel="web", user_id=web_user_id)
        asyncio.create_task(_web_auto_run())
        return {"session_id": router.format_session_id("web", web_user_id), "status": "processing"}

    # ===== Chat Stream =====
    @app.post("/api/chat/stream")
    async def chat_stream(request: Request):
        if not ws.agent:
            return JSONResponse({"error": "Agent not initialized"}, status_code=503)
        try:
            auth = await decode_jwt(request.headers.get("Authorization", "")[7:]) if os.environ.get("WEBUI_DISABLE_AUTH") != "1" else {"uid": 1, "name": "test", "role": "admin"}
        except Exception:
            auth = {"uid": "anon", "name": "匿名用户"}
        data = await request.json()
        if not data or not data.get("message"):
            return JSONResponse({"error": "Missing message"}, status_code=400)
        message = data["message"].strip()
        router = MessageRouter(ws.agent)
        session_id = data.get("session_id") or router.format_session_id("web", uuid.uuid4().hex[:8])
        chat_session = ws._get_or_create_session(session_id)
        chat_session.add_message("user", message)
        chat_session.is_streaming = True

        web_user_id = f"web:{auth['uid']}"

        stream_run_id = uuid.uuid4().hex
        agent_ref = ws.agent

        async def event_stream():
            from hooks import HookEvent
            q = asyncio.Queue()
            full_response: list[str] = []

            async def chat_handler(ctx):
                if ctx.token:
                    full_response.append(ctx.token)
                    await q.put(("token", ctx.token))

            async def event_handler(ctx):
                d: dict = {}
                if ctx.token:
                    d["content"] = ctx.token
                if ctx.tool_name:
                    d["name"] = ctx.tool_name
                if ctx.result:
                    d["result"] = ctx.result
                if ctx.agent_name:
                    d["agent_name"] = ctx.agent_name
                if ctx.agent_type:
                    d["agent_type"] = ctx.agent_type
                if ctx.metadata:
                    d.update(ctx.metadata)
                await q.put(("tool_event", {"event_type": ctx.event.value, "data": d}))

            hook_events = [
                HookEvent.CHAT_EVENT,
                HookEvent.TOOL_START,
                HookEvent.TOOL_RESULT,
                HookEvent.ROUND_START,
                HookEvent.SUBAGENT_START,
                HookEvent.SUBAGENT_RESULT,
                HookEvent.SUBAGENT_CHAT_EVENT,
                HookEvent.SUBAGENT_TOOL_START,
                HookEvent.SUBAGENT_TOOL_RESULT,
                HookEvent.SUBAGENT_ROUND_START,
            ]
            for evt in hook_events:
                agent_ref.hooks.register(
                    evt,
                    chat_handler if evt == HookEvent.CHAT_EVENT else event_handler,
                    run_id=stream_run_id,
                )

            async def run_agent():
                try:
                    loop = asyncio.get_running_loop()
                    future = loop.create_future()
                    router.on_response("web", web_user_id, future.set_result)
                    router.publish(
                        message, channel="web", user_id=web_user_id, run_id=stream_run_id)
                    await future
                    resp_text = "".join(full_response) if full_response else ""
                    await q.put(("done", resp_text))
                except Exception as e:
                    await q.put(("error", str(e)))
                finally:
                    for evt in hook_events:
                        agent_ref.hooks.unregister(
                            evt,
                            chat_handler if evt == HookEvent.CHAT_EVENT else event_handler,
                        )
                    chat_session.is_streaming = False

            agent_task = asyncio.create_task(run_agent())
            try:
                while True:
                    try:
                        event_type, content = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        if agent_task.done():
                            exc = agent_task.exception()
                            if exc:
                                yield _sse({"type": "error", "content": str(exc)})
                            break
                        yield _sse({"type": "heartbeat"})
                        continue

                    if event_type == "token":
                        yield _sse({"type": "token", "content": content})
                    elif event_type == "tool_event":
                        yield _sse({"type": content["event_type"], "data": content["data"]})
                    elif event_type == "done":
                        chat_session.add_message("assistant", content)
                        yield _sse({"type": "done", "content": content})
                        break
                    elif event_type == "error":
                        chat_session.add_message("assistant", f"Error: {content}")
                        yield _sse({"type": "error", "content": content})
                        break
            finally:
                if not agent_task.done():
                    agent_task.cancel()
                chat_session.is_streaming = False

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # ===== Tasks =====
    @app.get("/api/tasks")
    async def list_tasks():
        if not ws.agent or not ws.agent.task_manager:
            return {"tasks": [], "count": 0}
        tasks = ws.agent.task_manager.list_tasks()
        return {"tasks": tasks, "count": len(tasks)}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        if not ws.agent or not ws.agent.task_manager:
            raise HTTPException(404, "Not found")
        task = ws.agent.task_manager.get_task(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return {
            "id": task.id, "description": task.description,
            "status": task.status, "created_at": task.created_at,
            "result": task.result, "error": task.error,
        }

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        if not ws.agent or not ws.agent.task_manager:
            raise HTTPException(404, "Not found")
        success = await ws.agent.task_manager.cancel_task(task_id)
        return {"success": success, "task_id": task_id}

    # ===== Sessions =====
    @app.get("/api/sessions")
    async def list_sessions():
        with ws._session_lock:
            sessions = [{
                "id": sid, "created_at": s.created_at,
                "message_count": s.message_count(),
                "is_streaming": s.is_streaming,
            } for sid, s in ws._sessions.items()]
        return {"sessions": sessions}

    @app.get("/api/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        with ws._session_lock:
            chat_session = ws._sessions.get(session_id)
        if not chat_session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        return {"messages": chat_session.snapshot()}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str):
        with ws._session_lock:
            ws._sessions.pop(session_id, None)
        return {"success": True}

    # ===== Kanban =====
    @app.get("/api/kanban")
    async def kanban_list():
        if not ws._kanban:
            return JSONResponse({"error": "Kanban not available", "code": 503}, status_code=503)
        try:
            stats = ws._kanban.get_stats()
            tasks = ws._kanban.list_tasks()
            return {"stats": stats, "tasks": [t.to_dict() for t in tasks]}
        except Exception as e:
            logger.exception("Kanban API error")
            return JSONResponse({"error": str(e), "code": 500}, status_code=500)

    @app.post("/api/kanban")
    async def kanban_add(request: Request):
        if not ws._kanban:
            return JSONResponse({"error": "Kanban not available"}, status_code=503)
        data = await request.json()
        if not data or "title" not in data:
            return JSONResponse({"error": "Missing title"}, status_code=400)
        task = ws._kanban.add_task(
            title=data["title"],
            description=data.get("description", ""),
            priority=data.get("priority", 3),
            column=data.get("column", "backlog"),
            source="user",
            tags=data.get("tags"),
            interval=data.get("interval"),
        )
        return {"task": task.to_dict()}

    @app.patch("/api/kanban/{task_id}")
    async def kanban_update(task_id: str, request: Request):
        if not ws._kanban:
            return JSONResponse({"error": "Kanban not available"}, status_code=503)
        data = await request.json()
        if not data:
            return JSONResponse({"error": "Missing body"}, status_code=400)
        if "column" in data:
            ws._kanban.move_task(task_id, data["column"], data.get("assignee"))
        if "assignee" in data and "column" not in data:
            task = ws._kanban.get_task(task_id)
            if task:
                ws._kanban.move_task(task_id, task.column, assignee=data["assignee"])
        return {"success": True}

    @app.delete("/api/kanban/{task_id}")
    async def kanban_remove(task_id: str):
        if not ws._kanban:
            return JSONResponse({"error": "Kanban not available"}, status_code=503)
        if ws._kanban.remove_task(task_id):
            return {"success": True}
        return JSONResponse({"error": "Task not found"}, status_code=404)

    @app.post("/api/kanban/{task_id}/move")
    async def kanban_move(task_id: str, request: Request):
        if not ws._kanban:
            return JSONResponse({"error": "Kanban not available"}, status_code=503)
        data = await request.json()
        if not data or "column" not in data:
            return JSONResponse({"error": "Missing column"}, status_code=400)
        ok = ws._kanban.move_task(task_id, data["column"])
        return {"success": ok}

    # ===== Panel (compat) =====
    @app.get("/api/panel")
    async def panel_list_compat():
        if not ws._kanban:
            return JSONResponse({"error": "Panel not available", "code": 503}, status_code=503)
        try:
            stats = ws._kanban.get_stats()
            tasks = ws._kanban.list_tasks()
            status_map = {"backlog": "pending", "todo": "pending", "in_progress": "active", "done": "completed"}
            compat_tasks = []
            for t in tasks:
                d = t.to_dict()
                d["status"] = status_map.get(d["column"], "pending")
                compat_tasks.append(d)
            return {"stats": stats, "tasks": compat_tasks}
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    # ===== Todos =====
    @app.get("/api/todos")
    async def todo_list(status: str = Query("active")):
        if not ws.agent or not ws.agent.tool_registry:
            return JSONResponse({"error": "Agent not initialized"}, status_code=503)
        main_todos = []
        sub_todos = []
        seen_ids = set()

        todo_tool = ws.agent.tool_registry.get_tool("todowrite")
        if todo_tool:
            for t in todo_tool.get_todos("all"):
                if status == "active" and t.get("status") in ("completed", "cancelled"):
                    continue
                t["agent_id"] = ws.agent.name or "main"
                tid = t.get("id")
                if tid and tid not in seen_ids:
                    seen_ids.add(tid)
                    main_todos.append(t)

        if ws.agent.subagent_manager:
            try:
                for inst in list(ws.agent.subagent_manager._active_subagents.values()):
                    sub_agent = inst.agent
                    if sub_agent and sub_agent.tool_registry:
                        sub_todo = sub_agent.tool_registry.get_tool("todowrite")
                        if sub_todo:
                            agent_name = sub_agent.name or sub_agent.agent_id or inst.template or "sub"
                            for t in sub_todo.get_todos("all"):
                                if status == "active" and t.get("status") in ("completed", "cancelled"):
                                    continue
                                t["agent_id"] = agent_name
                                tid = t.get("id")
                                if tid and tid not in seen_ids:
                                    seen_ids.add(tid)
                                    sub_todos.append(t)
            except Exception as e:
                logger.warning(f"[Todos API] 遍历子代理todo失败: {e}")

        all_todos = sub_todos + main_todos
        return {"todos": all_todos, "count": len(all_todos)}

    # ===== Agent Sessions API =====
    @app.get("/api/agent/sessions")
    async def agent_sessions_list():
        if not ws.agent or not ws.agent.session_manager:
            return JSONResponse({"error": "Session manager not initialized"}, status_code=503)
        sessions = []
        try:
            for sid, sess in list(ws.agent.session_manager.sessions.items()):
                sessions.append({
                    "id": sid,
                    "agent_id": ws.agent.name or "main",
                    "messages": len(sess.messages),
                    "last_accessed": sess.last_accessed.isoformat(),
                })
        except Exception as e:
            logger.warning(f"[Sessions API] 读取主agent sessions失败: {e}")

        if ws.agent.subagent_manager:
            try:
                active = list(ws.agent.subagent_manager._active_subagents.values())
                for inst in active:
                    sub_agent = inst.agent
                    if not sub_agent or not sub_agent.session_manager:
                        continue
                    agent_name = sub_agent.name or sub_agent.agent_id or inst.template or "sub"
                    try:
                        sub_sessions = list(sub_agent.session_manager.sessions.items())
                    except Exception as e:
                        logger.warning(f"[Sessions API] 读取子代理 {agent_name} sessions失败: {e}")
                        continue
                    for ssid, sess in sub_sessions:
                        sessions.append({
                            "id": ssid,
                            "agent_id": agent_name,
                            "messages": len(sess.messages),
                            "last_accessed": sess.last_accessed.isoformat(),
                        })
            except Exception as e:
                logger.warning(f"[Sessions API] 遍历子代理失败: {e}")

        return {"total": len(sessions), "sessions": sessions}

    @app.get("/api/agent/sessions/agents")
    async def agent_sessions_agents():
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        return {"agents": storage.list_session_agents()}

    @app.get("/api/agent/sessions/history")
    async def agent_sessions_history(limit: int = Query(20), agent_id: str = Query("")):
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        rows = storage.list_recent_sessions(min(max(limit, 1), 200), agent_id=agent_id)
        sessions = [{
            "id": r["session_id"],
            "agent_id": r.get("agent_id") or "",
            "messages": r["msg_count"],
            "last_accessed": r["last_at"],
            "first_accessed": r["first_at"],
        } for r in rows]
        return {"total": len(sessions), "sessions": sessions}

    @app.get("/api/agent/sessions/messages")
    async def agent_session_messages(session_id: str = Query(...)):
        if not ws.agent or not ws.agent.session_manager:
            return JSONResponse({"error": "Session manager not initialized"}, status_code=503)
        session = ws.agent.session_manager.sessions.get(session_id)
        agent_name = ws.agent.name or "main"

        if not session and ws.agent.subagent_manager:
            try:
                for inst in list(ws.agent.subagent_manager._active_subagents.values()):
                    sub_agent = inst.agent
                    if sub_agent and sub_agent.session_manager:
                        sess = sub_agent.session_manager.sessions.get(session_id)
                        if sess:
                            session = sess
                            agent_name = sub_agent.name or sub_agent.agent_id or inst.template or "sub"
                            break
            except Exception as e:
                logger.warning(f"[Sessions API] 查找子代理session失败: {e}")

        if not session:
            storage = get_storage()
            if storage:
                db_msgs = storage.get_messages(session_id)
                if db_msgs:
                    return {"session_id": session_id, "agent_id": "(history)", "messages": db_msgs, "count": len(db_msgs)}
            return JSONResponse({"error": "Session not found"}, status_code=404)

        msgs = []
        for m in session.messages:
            if isinstance(m, dict):
                msg = {"role": m.get("role", ""), "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = m["tool_calls"]
                if m.get("tool_call_id"):
                    msg["tool_call_id"] = m["tool_call_id"]
                if m.get("name"):
                    msg["name"] = m["name"]
                msgs.append(msg)
        return {"session_id": session_id, "agent_id": agent_name, "messages": msgs, "count": len(msgs)}

    # ===== Agents CRUD =====
    @app.get("/api/agents")
    async def list_agents():
        config_dir = ws.agent.config_dir if ws.agent else ""
        agents_dir = os.path.join(config_dir, "agents") if config_dir else ""
        if not agents_dir or not os.path.isdir(agents_dir):
            return []
        result = []
        for dir_name in os.listdir(agents_dir):
            agent_path = os.path.join(agents_dir, dir_name)
            if not os.path.isdir(agent_path):
                continue
            prompt_file = os.path.join(agent_path, "PROMPT.md")
            if not os.path.exists(prompt_file):
                continue
            skills = []
            skills_dir = os.path.join(agent_path, "skills")
            if os.path.isdir(skills_dir):
                for sdir in os.listdir(skills_dir):
                    if os.path.isfile(os.path.join(skills_dir, sdir, "SKILL.md")):
                        skills.append(sdir)
            result.append({"name": dir_name, "dir": dir_name, "skills": skills})
        return result

    def _agent_prompt_file(name: str) -> str | None:
        config_dir = ws.agent.config_dir if ws.agent else ""
        if not config_dir:
            return None
        p = os.path.join(config_dir, "agents", name, "PROMPT.md")
        return p if os.path.isfile(p) else None

    @app.get("/api/agents/{name}/prompt")
    async def get_agent_prompt(name: str):
        prompt_file = _agent_prompt_file(name)
        if not prompt_file:
            raise HTTPException(404, "Not found")
        with open(prompt_file, encoding="utf-8") as f:
            return {"content": f.read()}

    @app.put("/api/agents/{name}/prompt")
    async def update_agent_prompt(name: str, request: Request):
        prompt_file = _agent_prompt_file(name)
        if not prompt_file:
            raise HTTPException(404, "Not found")
        data = await request.json()
        if not data or "content" not in data:
            return JSONResponse({"error": "Missing content"}, status_code=400)
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(data["content"])
        if ws.agent and ws.agent.subagent_manager:
            ws.agent.subagent_manager.reload_template(name)
        return {"success": True}

    def _skills_dir_for(name: str) -> str | None:
        config_dir = ws.agent.config_dir if ws.agent else ""
        if not config_dir:
            return None
        d = os.path.join(config_dir, "agents", name, "skills")
        return d if os.path.isdir(d) else None

    @app.get("/api/agents/{name}/skills")
    async def list_agent_skills(name: str):
        skills_dir = _skills_dir_for(name)
        if not skills_dir:
            return []
        result = []
        for sdir in os.listdir(skills_dir):
            if os.path.isfile(os.path.join(skills_dir, sdir, "SKILL.md")):
                result.append(sdir)
        return result

    @app.get("/api/agents/{name}/skills/{skill_name}")
    async def get_agent_skill(name: str, skill_name: str):
        skills_dir = _skills_dir_for(name)
        if not skills_dir:
            raise HTTPException(404, "Not found")
        skill_file = os.path.join(skills_dir, skill_name, "SKILL.md")
        if not os.path.isfile(skill_file):
            raise HTTPException(404, "Not found")
        with open(skill_file, encoding="utf-8") as f:
            return {"content": f.read()}

    @app.put("/api/agents/{name}/skills/{skill_name}")
    async def update_agent_skill(name: str, skill_name: str, request: Request):
        skills_dir = _skills_dir_for(name)
        if not skills_dir:
            raise HTTPException(404, "Not found")
        skill_file = os.path.join(skills_dir, skill_name, "SKILL.md")
        if not os.path.isfile(skill_file):
            raise HTTPException(404, "Not found")
        data = await request.json()
        if not data or "content" not in data:
            return JSONResponse({"error": "Missing content"}, status_code=400)
        with open(skill_file, "w", encoding="utf-8") as f:
            f.write(data["content"])
        return {"success": True}

    @app.post("/api/agents/{name}/skills")
    async def create_agent_skill(name: str, request: Request):
        skills_dir = _skills_dir_for(name)
        if not skills_dir:
            return JSONResponse({"error": "Agent not found"}, status_code=404)
        data = await request.json()
        if not data or "name" not in data:
            return JSONResponse({"error": "Missing name"}, status_code=400)
        skill_dir = os.path.join(skills_dir, data["name"])
        if os.path.exists(skill_dir):
            return JSONResponse({"error": "Skill already exists"}, status_code=409)
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(data.get("content", ""))
        return {"success": True}

    @app.delete("/api/agents/{name}/skills/{skill_name}")
    async def delete_agent_skill(name: str, skill_name: str):
        import shutil
        skills_dir = _skills_dir_for(name)
        if not skills_dir:
            raise HTTPException(404, "Not found")
        skill_dir = os.path.join(skills_dir, skill_name)
        if not os.path.isdir(skill_dir):
            raise HTTPException(404, "Not found")
        shutil.rmtree(skill_dir)
        return {"success": True}

    # ===== RBAC API =====
    def _get_rbac():
        if not ws.agent or not ws.agent.rbac:
            return None
        return ws.agent.rbac

    def _require_rbac():
        rbac = _get_rbac()
        if not rbac:
            raise HTTPException(503, "RBAC not initialized")
        return rbac

    async def _require_rbac_admin(request: Request):
        await get_admin(request)
        return _require_rbac()

    @app.get("/api/rbac/roles")
    async def rbac_list_roles():
        return {"roles": _require_rbac().list_roles()}

    @app.post("/api/rbac/roles")
    async def rbac_create_role(request: Request):
        await get_admin(request)
        rbac = _require_rbac()
        data = await request.json()
        if not data or "name" not in data:
            return JSONResponse({"error": "Missing name"}, status_code=400)
        rbac.create_role(name=data["name"], description=data.get("description", ""),
                         allowed_tools=data.get("allowed_tools", []),
                         allowed_agents=data.get("allowed_agents", []))
        return {"success": True, "name": data["name"]}

    @app.get("/api/rbac/roles/{name}")
    async def rbac_get_role(name: str):
        rbac = _require_rbac()
        role = rbac.get_role(name)
        if not role:
            raise HTTPException(404, "Role not found")
        return role

    @app.put("/api/rbac/roles/{name}")
    async def rbac_update_role(name: str, request: Request):
        await get_admin(request)
        rbac = _require_rbac()
        data = await request.json()
        if not data:
            return JSONResponse({"error": "Missing body"}, status_code=400)
        rbac.update_role(name=name, description=data.get("description"),
                         allowed_tools=data.get("allowed_tools"),
                         allowed_agents=data.get("allowed_agents"))
        return {"success": True}

    @app.delete("/api/rbac/roles/{name}")
    async def rbac_delete_role(name: str):
        rbac = _require_rbac()
        if not rbac.delete_role(name):
            return JSONResponse({"error": "Cannot delete built-in role"}, status_code=400)
        return {"success": True}

    @app.get("/api/rbac/users")
    async def rbac_list_users():
        rbac = _require_rbac()
        users = rbac.list_users_with_password_flag()
        for u in users:
            u["identities"] = rbac.list_user_identities(u["id"])
        return {"users": users}

    @app.post("/api/rbac/users")
    async def rbac_create_user(request: Request):
        await get_admin(request)
        rbac = _require_rbac()
        data = await request.json()
        if not data or "name" not in data:
            return JSONResponse({"error": "Missing name"}, status_code=400)
        user_id = rbac.create_user(name=data["name"], department=data.get("department", ""),
                                   role=data.get("role", "default"))
        pw = (data.get("password") or "").strip()
        if pw:
            get_storage().set_user_password(user_id, pw)
        for ident in data.get("identities", []):
            if ident.get("platform") and ident.get("platform_uid"):
                rbac.bind_identity(user_id, ident["platform"], ident["platform_uid"])
        user = rbac.get_user(user_id)
        user["identities"] = rbac.list_user_identities(user_id)
        return {"success": True, "user": user}

    @app.get("/api/rbac/users/{user_id}")
    async def rbac_get_user(user_id: int):
        rbac = _require_rbac()
        user = rbac.get_user_with_password_flag(user_id)
        if not user:
            raise HTTPException(404, "User not found")
        user["identities"] = rbac.list_user_identities(user_id)
        return user

    @app.put("/api/rbac/users/{user_id}")
    async def rbac_update_user(user_id: int, request: Request):
        await get_admin(request)
        rbac = _require_rbac()
        data = await request.json()
        if not data:
            return JSONResponse({"error": "Missing body"}, status_code=400)
        rbac.update_user(user_id=user_id, name=data.get("name"),
                         department=data.get("department"), role=data.get("role"))
        pw = (data.get("password") or "").strip()
        if pw:
            get_storage().set_user_password(user_id, pw)
        return {"success": True}

    @app.delete("/api/rbac/users/{user_id}")
    async def rbac_delete_user(user_id: int):
        _require_rbac().delete_user(user_id)
        return {"success": True}

    @app.post("/api/rbac/users/{user_id}/toggle")
    async def rbac_toggle_user(user_id: int):
        rbac = _require_rbac()
        user = rbac.get_user(user_id)
        if not user:
            raise HTTPException(404, "User not found")
        if user["status"] == "active":
            rbac.disable_user(user_id)
        else:
            rbac.enable_user(user_id)
        user = rbac.get_user(user_id)
        return {"success": True, "status": user["status"]}

    @app.post("/api/rbac/users/{user_id}/identities")
    async def rbac_bind_identity(user_id: int, request: Request):
        await get_admin(request)
        rbac = _require_rbac()
        data = await request.json()
        if not data or "platform" not in data or "platform_uid" not in data:
            return JSONResponse({"error": "Missing platform or platform_uid"}, status_code=400)
        rbac.bind_identity(user_id, data["platform"], data["platform_uid"])
        return {"success": True}

    @app.delete("/api/rbac/identities/{identity_id}")
    async def rbac_unbind_identity(identity_id: int):
        _require_rbac().unbind_identity(identity_id)
        return {"success": True}

    # ===== Memory Management API =====
    @app.get("/api/memories")
    async def list_memories(scope: str = Query(""), category: str = Query(""),
                            owner_id: str = Query(""), q: str = Query(""),
                            limit: int = Query(100), offset: int = Query(0)):
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        rows = storage.list_memories(scope=scope, owner_id=owner_id, category=category,
                                     keyword=q, limit=min(max(limit, 1), 500), offset=max(offset, 0))
        total = storage.count_memories(scope=scope, owner_id=owner_id, category=category, keyword=q)
        return {"memories": rows, "total": total}

    @app.post("/api/memories")
    async def create_memory(request: Request):
        await get_admin(request)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        data = await request.json()
        if not data or not (data.get("content") or "").strip():
            return JSONResponse({"error": "Missing content"}, status_code=400)
        mid = storage.save_memory(scope=data.get("scope", "global"), owner_id=data.get("owner_id", ""),
                                  category=data.get("category", "knowledge"),
                                  content=data.get("content", ""), agent_id=data.get("agent_id", ""),
                                  source=data.get("source", "admin"), importance=data.get("importance", 3))
        return {"id": mid, "success": True}

    @app.put("/api/memories/{memory_id}")
    async def update_memory(memory_id: int, request: Request):
        await get_admin(request)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        data = await request.json()
        if not data:
            return JSONResponse({"error": "Missing body"}, status_code=400)
        ok = storage.update_memory(memory_id, content=data.get("content"),
                                   importance=data.get("importance"), category=data.get("category"),
                                   scope=data.get("scope"), owner_id=data.get("owner_id"))
        return {"success": ok}

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: int, request: Request):
        await get_admin(request)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=503)
        ok = storage.delete_memory(memory_id)
        if not ok:
            return JSONResponse({"error": "Memory not found"}, status_code=404)
        return {"success": True}

    # ===== Memory Proposals API =====
    @app.get("/api/memory/proposals")
    async def memory_list_proposals(status: str = Query("pending")):
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=500)
        return {"proposals": storage.list_proposals(status)}

    @app.post("/api/memory/proposals/{pid}/approve")
    async def memory_approve(pid: int, request: Request):
        await get_admin(request)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=500)
        p = storage.get_proposal(pid)
        if not p or p["status"] != "pending":
            return JSONResponse({"error": "invalid proposal"}, status_code=400)
        storage.save_memory(scope="global", owner_id="", category="knowledge",
                            content=p["content"], source="admin")
        storage.update_proposal_status(pid, "approved", "admin")
        return {"success": True}

    @app.post("/api/memory/proposals/{pid}/reject")
    async def memory_reject(pid: int, request: Request):
        await get_admin(request)
        storage = get_storage()
        if not storage:
            return JSONResponse({"error": "storage unavailable"}, status_code=500)
        storage.update_proposal_status(pid, "rejected", "admin")
        return {"success": True}

    # ===== Workspace Files =====
    @app.get("/api/workspace/files")
    async def workspace_files():
        if not ws.agent:
            return JSONResponse({"error": "Agent not initialized"}, status_code=503)
        ws_dir = ws.agent.workspace
        if not os.path.isdir(ws_dir):
            return {"files": []}
        entries = []
        for fname in os.listdir(ws_dir):
            fpath = os.path.join(ws_dir, fname)
            if os.path.isfile(fpath):
                stat = os.stat(fpath)
                entries.append({
                    "name": fname,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
        entries.sort(key=lambda e: e["modified"], reverse=True)
        return {"files": entries}

    # ===== Log Stream =====
    @app.get("/api/logs/stream")
    async def log_stream():
        q = ws._log_handler.subscribe()

        async def gen():
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(q.get(), timeout=5)
                        yield _sse({"line": line})
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat"})
            finally:
                ws._log_handler.unsubscribe(q)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
