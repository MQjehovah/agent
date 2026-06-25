import asyncio
import contextlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("agent.web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _sse(payload: dict) -> str:
    """格式化一条 SSE 事件"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class LogStreamHandler(logging.Handler):
    """把日志记录广播给所有 SSE 订阅者。

    日志可能来自任意线程（主事件循环 / 存储写线程 / 钉钉 stream 等），
    故用 loop.call_soon_threadsafe 把投递动作调度回事件循环线程，
    确保 asyncio.Queue 的操作线程安全。
    """

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
            q.put_nowait(line)  # 订阅者消费过慢则丢弃，防止积压撑爆内存


class ChatSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.created_at = datetime.now().isoformat()
        self.is_streaming = False
        self.messages: list[dict[str, Any]] = []
        # 保护 messages 列表：并发请求下 append/读取会竞态（迭代时被修改报错）
        self._lock = threading.Lock()

    def add_message(self, role: str, content: str):
        with self._lock:
            self.messages.append({"role": role, "content": content, "time": datetime.now().isoformat()})

    def message_count(self) -> int:
        with self._lock:
            return len(self.messages)

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.messages)


class WebServer:
    """FastAPI Web 服务。

    与旧 Flask 版本的关键差异：整个服务运行在 agent 的 asyncio 事件循环内，
    路由为 async，可直接 `await agent.run(...)`，彻底消除了原先
    独立线程 + asyncio.run_coroutine_threadsafe 跨线程调用的复杂度与隐患。
    """

    MAX_WEB_SESSIONS = 500

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, loop: asyncio.AbstractEventLoop = None):
        self.host = host
        self.port = port
        self.agent = None
        self._kanban = None
        self.loop: asyncio.AbstractEventLoop | None = loop
        self._app: FastAPI = FastAPI(title="Agent Web UI", docs_url="/api/docs")
        self._server: uvicorn.Server | None = None
        self._sessions: dict[str, ChatSession] = {}
        self._session_lock = threading.Lock()
        # 日志流 handler：把 agent 日志广播给 /api/logs/stream 订阅者
        self._log_handler = LogStreamHandler()
        self._log_handler.setFormatter(
            logging.Formatter("[%(name)s] %(levelname)s %(message)s")
        )
        self._setup_routes()

    def set_agent(self, agent):
        self.agent = agent

    def set_panel(self, panel):
        self._kanban = panel

    def set_kanban(self, kanban_board):
        self._kanban = kanban_board

    def _get_or_create_session(self, session_id: str) -> ChatSession:
        """获取或创建 Web 会话，带线程安全与上限淘汰（防止内存无限增长）。"""
        with self._session_lock:
            if session_id not in self._sessions:
                if len(self._sessions) >= self.MAX_WEB_SESSIONS:
                    oldest = min(self._sessions.values(), key=lambda s: s.created_at)
                    self._sessions.pop(oldest.session_id, None)
                self._sessions[session_id] = ChatSession(session_id)
            return self._sessions[session_id]

    def start(self):
        """在当前 asyncio 事件循环内启动 uvicorn（非阻塞后台 task）。"""
        loop = self.loop or asyncio.get_event_loop()
        config = uvicorn.Config(
            self._app, host=self.host, port=self.port,
            log_level="warning", loop="asyncio", access_log=False,
        )
        self._server = uvicorn.Server(config)
        # 嵌入式运行：禁用 uvicorn 自带的信号处理，统一由 main.py 控制
        self._server.install_signal_handlers = lambda: None
        # 绑定事件循环并把日志流 handler 挂到 agent logger（含所有子 logger）
        self._log_handler.set_loop(loop)
        logging.getLogger("agent").addHandler(self._log_handler)
        loop.create_task(self._server.serve())
        logger.info(f"Web UI (FastAPI/uvicorn) started at http://{self.host}:{self.port}")

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        logger.info("WebServer stopped")

    # ------------------------------------------------------------------ #
    #  路由
    # ------------------------------------------------------------------ #

    def _setup_routes(self):
        from storage import get_storage

        # 静态资源
        if os.path.isdir(STATIC_DIR):
            self._app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        # 首页：注入静态资源版本号（基于 mtime），避免浏览器缓存旧版 app.js / style.css
        _index_cache: dict = {}

        @self._app.get("/")
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
            # 给静态资源加 ?v=mtime，文件一变浏览器自动重新拉取
            for name in ("app.js", "style.css"):
                p = os.path.join(STATIC_DIR, name)
                if os.path.isfile(p):
                    mt = int(os.path.getmtime(p))
                    html = html.replace(f"/static/{name}", f"/static/{name}?v={mt}")
            _index_cache.clear()
            _index_cache[key] = html
            return HTMLResponse(html)

        @self._app.get("/api/agent/status")
        async def agent_status():
            if not self.agent:
                return JSONResponse({"error": "Agent not initialized"}, status_code=503)
            task_mgr = self.agent.task_manager
            tasks = task_mgr.list_tasks() if task_mgr else []
            status_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0, "cancelled": 0}
            for t in tasks:
                s = t.get("status", "pending")
                if s in status_counts:
                    status_counts[s] += 1
            usage = self.agent.client.usage_tracker.get_summary() if self.agent.client else {}

            subagents = []
            if self.agent.subagent_manager:
                for name in self.agent.subagent_manager.list_templates():
                    tmpl = self.agent.subagent_manager.get_template(name)
                    desc = (tmpl.get("description", "") or "")[:80] if tmpl else ""
                    subagents.append({"name": name, "description": desc})

            panel_stats = {}
            if self._kanban:
                panel_stats = self._kanban.get_stats()

            return {
                "name": self.agent.name or "Agent",
                "description": self.agent.description or "",
                "status": self.agent.status,
                "model": self.agent.client.model if self.agent.client else "",
                "tasks": status_counts,
                "usage": usage,
                "tools": self.agent.tool_registry.list_tools() if self.agent.tool_registry else [],
                "subagents": subagents,
                "panel": panel_stats,
            }

        @self._app.post("/api/chat")
        async def chat(request: Request):
            if not self.agent:
                return JSONResponse({"error": "Agent not initialized"}, status_code=503)
            data = await request.json()
            if not data or not data.get("message"):
                return JSONResponse({"error": "Missing message"}, status_code=400)

            message = data["message"].strip()
            if not message:
                return JSONResponse({"error": "Empty message"}, status_code=400)

            session_id = data.get("session_id") or f"web_{uuid.uuid4().hex[:8]}"
            chat_session = self._get_or_create_session(session_id)
            chat_session.add_message("user", message)
            chat_session.is_streaming = True

            # 非流式：后台执行，立即返回 session_id
            asyncio.create_task(
                self.agent.run(message, session_id=session_id)
            )
            return {"session_id": session_id, "status": "processing"}

        @self._app.post("/api/chat/stream")
        async def chat_stream(request: Request):
            if not self.agent:
                return JSONResponse({"error": "Agent not initialized"}, status_code=503)
            data = await request.json()
            if not data or not data.get("message"):
                return JSONResponse({"error": "Missing message"}, status_code=400)

            message = data["message"].strip()
            session_id = data.get("session_id") or f"web_{uuid.uuid4().hex[:8]}"
            chat_session = self._get_or_create_session(session_id)
            chat_session.add_message("user", message)
            chat_session.is_streaming = True

            # 本次流式请求的唯一 run_id，把流式事件限定在本请求内，杜绝并发串流
            stream_run_id = uuid.uuid4().hex
            agent_ref = self.agent

            async def event_stream():
                from hooks import HookEvent
                q: asyncio.Queue = asyncio.Queue()
                full_response: list[str] = []

                async def chat_handler(ctx):
                    if ctx.token:
                        full_response.append(ctx.token)
                        await q.put(("token", ctx.token))

                async def event_handler(ctx):
                    d: dict[str, Any] = {}
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
                        result = await agent_ref.run(message, session_id=session_id, run_id=stream_run_id)
                        resp_text = result.result if result and hasattr(result, "result") else ""
                        if full_response:
                            resp_text = "".join(full_response)
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

        @self._app.get("/api/tasks")
        async def list_tasks():
            if not self.agent or not self.agent.task_manager:
                return {"tasks": [], "count": 0}
            tasks = self.agent.task_manager.list_tasks()
            return {"tasks": tasks, "count": len(tasks)}

        @self._app.get("/api/tasks/{task_id}")
        async def get_task(task_id: str):
            if not self.agent or not self.agent.task_manager:
                raise HTTPException(404, "Not found")
            task = self.agent.task_manager.get_task(task_id)
            if not task:
                raise HTTPException(404, "Task not found")
            return {
                "id": task.id, "description": task.description,
                "status": task.status, "created_at": task.created_at,
                "result": task.result, "error": task.error,
            }

        @self._app.post("/api/tasks/{task_id}/cancel")
        async def cancel_task(task_id: str):
            if not self.agent or not self.agent.task_manager:
                raise HTTPException(404, "Not found")
            success = await self.agent.task_manager.cancel_task(task_id)
            return {"success": success, "task_id": task_id}

        @self._app.get("/api/sessions")
        async def list_sessions():
            with self._session_lock:
                sessions = [{
                    "id": sid, "created_at": s.created_at,
                    "message_count": s.message_count(),
                    "is_streaming": s.is_streaming,
                } for sid, s in self._sessions.items()]
            return {"sessions": sessions}

        @self._app.get("/api/sessions/{session_id}/messages")
        async def session_messages(session_id: str):
            with self._session_lock:
                chat_session = self._sessions.get(session_id)
            if not chat_session:
                return JSONResponse({"error": "Session not found"}, status_code=404)
            return {"messages": chat_session.snapshot()}

        @self._app.delete("/api/sessions/{session_id}")
        async def delete_session(session_id: str):
            with self._session_lock:
                self._sessions.pop(session_id, None)
            return {"success": True}

        # ===== 看板 API =====
        @self._app.get("/api/kanban")
        async def kanban_list():
            if not self._kanban:
                return JSONResponse({"error": "Kanban not available", "code": 503}, status_code=503)
            try:
                stats = self._kanban.get_stats()
                tasks = self._kanban.list_tasks()
                return {"stats": stats, "tasks": [t.to_dict() for t in tasks]}
            except Exception as e:
                logger.exception("Kanban API error")
                return JSONResponse({"error": str(e), "code": 500}, status_code=500)

        @self._app.post("/api/kanban")
        async def kanban_add(request: Request):
            if not self._kanban:
                return JSONResponse({"error": "Kanban not available"}, status_code=503)
            data = await request.json()
            if not data or "title" not in data:
                return JSONResponse({"error": "Missing title"}, status_code=400)
            task = self._kanban.add_task(
                title=data["title"],
                description=data.get("description", ""),
                priority=data.get("priority", 3),
                column=data.get("column", "backlog"),
                source="user",
                tags=data.get("tags"),
                interval=data.get("interval"),
            )
            return {"task": task.to_dict()}

        @self._app.patch("/api/kanban/{task_id}")
        async def kanban_update(task_id: str, request: Request):
            if not self._kanban:
                return JSONResponse({"error": "Kanban not available"}, status_code=503)
            data = await request.json()
            if not data:
                return JSONResponse({"error": "Missing body"}, status_code=400)
            if "column" in data:
                self._kanban.move_task(task_id, data["column"], data.get("assignee"))
            if "assignee" in data and "column" not in data:
                task = self._kanban.get_task(task_id)
                if task:
                    self._kanban.move_task(task_id, task.column, assignee=data["assignee"])
            return {"success": True}

        @self._app.delete("/api/kanban/{task_id}")
        async def kanban_remove(task_id: str):
            if not self._kanban:
                return JSONResponse({"error": "Kanban not available"}, status_code=503)
            if self._kanban.remove_task(task_id):
                return {"success": True}
            return JSONResponse({"error": "Task not found"}, status_code=404)

        @self._app.post("/api/kanban/{task_id}/move")
        async def kanban_move(task_id: str, request: Request):
            if not self._kanban:
                return JSONResponse({"error": "Kanban not available"}, status_code=503)
            data = await request.json()
            if not data or "column" not in data:
                return JSONResponse({"error": "Missing column"}, status_code=400)
            ok = self._kanban.move_task(task_id, data["column"])
            return {"success": ok}

        # 兼容旧 API
        @self._app.get("/api/panel")
        async def panel_list_compat():
            if not self._kanban:
                return JSONResponse({"error": "Panel not available", "code": 503}, status_code=503)
            try:
                stats = self._kanban.get_stats()
                tasks = self._kanban.list_tasks()
                status_map = {"backlog": "pending", "todo": "pending", "in_progress": "active", "done": "completed"}
                compat_tasks = []
                for t in tasks:
                    d = t.to_dict()
                    d["status"] = status_map.get(d["column"], "pending")
                    compat_tasks.append(d)
                return {"stats": stats, "tasks": compat_tasks}
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=500)

        # Todo API
        @self._app.get("/api/todos")
        async def todo_list(status: str = Query("active")):
            if not self.agent or not self.agent.tool_registry:
                return JSONResponse({"error": "Agent not initialized"}, status_code=503)

            main_todos = []
            sub_todos = []
            seen_ids = set()

            todo_tool = self.agent.tool_registry.get_tool("todowrite")
            if todo_tool:
                for t in todo_tool.get_todos("all"):
                    if status == "active" and t.get("status") in ("completed", "cancelled"):
                        continue
                    t["agent_id"] = self.agent.name or "main"
                    tid = t.get("id")
                    if tid and tid not in seen_ids:
                        seen_ids.add(tid)
                        main_todos.append(t)

            if self.agent.subagent_manager:
                try:
                    for inst in list(self.agent.subagent_manager._active_subagents.values()):
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

        # Agent Sessions API (in-memory)
        @self._app.get("/api/agent/sessions")
        async def agent_sessions_list():
            if not self.agent or not self.agent.session_manager:
                return JSONResponse({"error": "Session manager not initialized"}, status_code=503)

            sessions = []
            try:
                for sid, sess in list(self.agent.session_manager.sessions.items()):
                    sessions.append({
                        "id": sid,
                        "agent_id": self.agent.name or "main",
                        "messages": len(sess.messages),
                        "last_accessed": sess.last_accessed.isoformat(),
                    })
            except Exception as e:
                logger.warning(f"[Sessions API] 读取主agent sessions失败: {e}")

            if self.agent.subagent_manager:
                try:
                    active = list(self.agent.subagent_manager._active_subagents.values())
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

        @self._app.get("/api/agent/sessions/history")
        async def agent_sessions_history(limit: int = Query(20)):
            """从数据库查询最近 N 个会话（不依赖内存活跃 session，重启后仍可见）"""
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=503)
            rows = storage.list_recent_sessions(min(max(limit, 1), 200))
            sessions = [{
                "id": r["session_id"],
                "agent_id": r.get("agent_id") or "",
                "messages": r["msg_count"],
                "last_accessed": r["last_at"],
                "first_accessed": r["first_at"],
            } for r in rows]
            return {"total": len(sessions), "sessions": sessions}

        @self._app.get("/api/agent/sessions/{session_id}/messages")
        async def agent_session_messages(session_id: str):
            if not self.agent or not self.agent.session_manager:
                return JSONResponse({"error": "Session manager not initialized"}, status_code=503)

            session = self.agent.session_manager.sessions.get(session_id)
            agent_name = self.agent.name or "main"

            if not session and self.agent.subagent_manager:
                try:
                    for inst in list(self.agent.subagent_manager._active_subagents.values()):
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
                # 内存未命中：从数据库恢复历史会话消息
                storage = get_storage()
                if storage:
                    db_msgs = storage.get_messages(session_id)
                    if db_msgs:
                        return {
                            "session_id": session_id,
                            "agent_id": "(history)",
                            "messages": db_msgs,
                            "count": len(db_msgs),
                        }
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

        @self._app.get("/api/agents")
        async def list_agents():
            config_dir = self.agent.config_dir if self.agent else ""
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
            config_dir = self.agent.config_dir if self.agent else ""
            if not config_dir:
                return None
            p = os.path.join(config_dir, "agents", name, "PROMPT.md")
            return p if os.path.isfile(p) else None

        @self._app.get("/api/agents/{name}/prompt")
        async def get_agent_prompt(name: str):
            prompt_file = _agent_prompt_file(name)
            if not prompt_file:
                raise HTTPException(404, "Not found")
            with open(prompt_file, encoding="utf-8") as f:
                return {"content": f.read()}

        @self._app.put("/api/agents/{name}/prompt")
        async def update_agent_prompt(name: str, request: Request):
            prompt_file = _agent_prompt_file(name)
            if not prompt_file:
                raise HTTPException(404, "Not found")
            data = await request.json()
            if not data or "content" not in data:
                return JSONResponse({"error": "Missing content"}, status_code=400)
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(data["content"])
            if self.agent and self.agent.subagent_manager:
                self.agent.subagent_manager.reload_template(name)
            return {"success": True}

        def _skills_dir_for(name: str) -> str | None:
            config_dir = self.agent.config_dir if self.agent else ""
            if not config_dir:
                return None
            d = os.path.join(config_dir, "agents", name, "skills")
            return d if os.path.isdir(d) else None

        @self._app.get("/api/agents/{name}/skills")
        async def list_agent_skills(name: str):
            skills_dir = _skills_dir_for(name)
            if not skills_dir:
                return []
            result = []
            for sdir in os.listdir(skills_dir):
                if os.path.isfile(os.path.join(skills_dir, sdir, "SKILL.md")):
                    result.append(sdir)
            return result

        @self._app.get("/api/agents/{name}/skills/{skill_name}")
        async def get_agent_skill(name: str, skill_name: str):
            skills_dir = _skills_dir_for(name)
            if not skills_dir:
                raise HTTPException(404, "Not found")
            skill_file = os.path.join(skills_dir, skill_name, "SKILL.md")
            if not os.path.isfile(skill_file):
                raise HTTPException(404, "Not found")
            with open(skill_file, encoding="utf-8") as f:
                return {"content": f.read()}

        @self._app.put("/api/agents/{name}/skills/{skill_name}")
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

        @self._app.post("/api/agents/{name}/skills")
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

        @self._app.delete("/api/agents/{name}/skills/{skill_name}")
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
            if not self.agent or not self.agent.rbac:
                return None
            return self.agent.rbac

        def _require_rbac():
            rbac = _get_rbac()
            if not rbac:
                raise HTTPException(503, "RBAC not initialized")
            return rbac

        @self._app.get("/api/rbac/roles")
        async def rbac_list_roles():
            return {"roles": _require_rbac().list_roles()}

        @self._app.post("/api/rbac/roles")
        async def rbac_create_role(request: Request):
            rbac = _require_rbac()
            data = await request.json()
            if not data or "name" not in data:
                return JSONResponse({"error": "Missing name"}, status_code=400)
            rbac.create_role(
                name=data["name"],
                description=data.get("description", ""),
                allowed_tools=data.get("allowed_tools", []),
                allowed_agents=data.get("allowed_agents", []),
            )
            return {"success": True, "name": data["name"]}

        @self._app.get("/api/rbac/roles/{name}")
        async def rbac_get_role(name: str):
            rbac = _require_rbac()
            role = rbac.get_role(name)
            if not role:
                raise HTTPException(404, "Role not found")
            return role

        @self._app.put("/api/rbac/roles/{name}")
        async def rbac_update_role(name: str, request: Request):
            rbac = _require_rbac()
            data = await request.json()
            if not data:
                return JSONResponse({"error": "Missing body"}, status_code=400)
            rbac.update_role(
                name=name,
                description=data.get("description"),
                allowed_tools=data.get("allowed_tools"),
                allowed_agents=data.get("allowed_agents"),
            )
            return {"success": True}

        @self._app.delete("/api/rbac/roles/{name}")
        async def rbac_delete_role(name: str):
            rbac = _require_rbac()
            if not rbac.delete_role(name):
                return JSONResponse({"error": "Cannot delete built-in role"}, status_code=400)
            return {"success": True}

        @self._app.get("/api/rbac/users")
        async def rbac_list_users():
            rbac = _require_rbac()
            users = rbac.list_users()
            for u in users:
                u["identities"] = rbac.list_user_identities(u["id"])
            return {"users": users}

        @self._app.post("/api/rbac/users")
        async def rbac_create_user(request: Request):
            rbac = _require_rbac()
            data = await request.json()
            if not data or "name" not in data:
                return JSONResponse({"error": "Missing name"}, status_code=400)
            user_id = rbac.create_user(
                name=data["name"],
                department=data.get("department", ""),
                role=data.get("role", "default"),
            )
            for ident in data.get("identities", []):
                if ident.get("platform") and ident.get("platform_uid"):
                    rbac.bind_identity(user_id, ident["platform"], ident["platform_uid"])
            user = rbac.get_user(user_id)
            user["identities"] = rbac.list_user_identities(user_id)
            return {"success": True, "user": user}

        @self._app.get("/api/rbac/users/{user_id}")
        async def rbac_get_user(user_id: int):
            rbac = _require_rbac()
            user = rbac.get_user(user_id)
            if not user:
                raise HTTPException(404, "User not found")
            user["identities"] = rbac.list_user_identities(user_id)
            return user

        @self._app.put("/api/rbac/users/{user_id}")
        async def rbac_update_user(user_id: int, request: Request):
            rbac = _require_rbac()
            data = await request.json()
            if not data:
                return JSONResponse({"error": "Missing body"}, status_code=400)
            rbac.update_user(
                user_id=user_id,
                name=data.get("name"),
                department=data.get("department"),
                role=data.get("role"),
            )
            return {"success": True}

        @self._app.delete("/api/rbac/users/{user_id}")
        async def rbac_delete_user(user_id: int):
            _require_rbac().delete_user(user_id)
            return {"success": True}

        @self._app.post("/api/rbac/users/{user_id}/toggle")
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

        @self._app.post("/api/rbac/users/{user_id}/identities")
        async def rbac_bind_identity(user_id: int, request: Request):
            rbac = _require_rbac()
            data = await request.json()
            if not data or "platform" not in data or "platform_uid" not in data:
                return JSONResponse({"error": "Missing platform or platform_uid"}, status_code=400)
            rbac.bind_identity(user_id, data["platform"], data["platform_uid"])
            return {"success": True}

        @self._app.delete("/api/rbac/identities/{identity_id}")
        async def rbac_unbind_identity(identity_id: int):
            _require_rbac().unbind_identity(identity_id)
            return {"success": True}

        # ===== Memory 管理 API（WebUI 后台，默认 admin 权限） =====
        @self._app.get("/api/memories")
        async def list_memories(scope: str = Query(""), category: str = Query(""),
                                owner_id: str = Query(""), q: str = Query(""),
                                limit: int = Query(100), offset: int = Query(0)):
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=503)
            rows = storage.list_memories(scope=scope, owner_id=owner_id,
                                          category=category, keyword=q,
                                          limit=min(max(limit, 1), 500), offset=max(offset, 0))
            total = storage.count_memories(scope=scope, owner_id=owner_id,
                                            category=category, keyword=q)
            return {"memories": rows, "total": total}

        @self._app.post("/api/memories")
        async def create_memory(request: Request):
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=503)
            data = await request.json()
            if not data or not (data.get("content") or "").strip():
                return JSONResponse({"error": "Missing content"}, status_code=400)
            mid = storage.save_memory(
                scope=data.get("scope", "global"),
                owner_id=data.get("owner_id", ""),
                category=data.get("category", "knowledge"),
                content=data.get("content", ""),
                agent_id=data.get("agent_id", ""),
                source=data.get("source", "admin"),
                importance=data.get("importance", 3),
            )
            return {"id": mid, "success": True}

        @self._app.put("/api/memories/{memory_id}")
        async def update_memory(memory_id: int, request: Request):
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=503)
            data = await request.json()
            if not data:
                return JSONResponse({"error": "Missing body"}, status_code=400)
            ok = storage.update_memory(
                memory_id,
                content=data.get("content"),
                importance=data.get("importance"),
                category=data.get("category"),
                scope=data.get("scope"),
                owner_id=data.get("owner_id"),
            )
            return {"success": ok}

        @self._app.delete("/api/memories/{memory_id}")
        async def delete_memory(memory_id: int):
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=503)
            ok = storage.delete_memory(memory_id)
            if not ok:
                return JSONResponse({"error": "Memory not found"}, status_code=404)
            return {"success": True}

        # ===== Memory Proposals API（仅 admin 可操作，待补鉴权中间件） =====
        @self._app.get("/api/memory/proposals")
        async def memory_list_proposals(status: str = Query("pending")):
            # TODO: 校验调用者为 admin 角色
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=500)
            return {"proposals": storage.list_proposals(status)}

        @self._app.post("/api/memory/proposals/{pid}/approve")
        async def memory_approve(pid: int):
            # TODO: 校验调用者为 admin 角色；reviewer 待鉴权后替换
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=500)
            p = storage.get_proposal(pid)
            if not p or p["status"] != "pending":
                return JSONResponse({"error": "invalid proposal"}, status_code=400)
            storage.save_memory(
                scope="global", owner_id="", category="knowledge",
                content=p["content"], source="admin",
            )
            storage.update_proposal_status(pid, "approved", "admin")
            return {"success": True}

        @self._app.post("/api/memory/proposals/{pid}/reject")
        async def memory_reject(pid: int):
            # TODO: 校验调用者为 admin 角色；reviewer 待鉴权后替换
            storage = get_storage()
            if not storage:
                return JSONResponse({"error": "storage unavailable"}, status_code=500)
            storage.update_proposal_status(pid, "rejected", "admin")
            return {"success": True}

        # ===== 日志流（SSE） =====
        @self._app.get("/api/logs/stream")
        async def log_stream():
            """实时推送 agent 日志给前端 Logs 标签页"""
            q = self._log_handler.subscribe()

            async def gen():
                try:
                    while True:
                        try:
                            line = await asyncio.wait_for(q.get(), timeout=5)
                            yield _sse({"line": line})
                        except asyncio.TimeoutError:
                            yield _sse({"type": "heartbeat"})
                finally:
                    self._log_handler.unsubscribe(q)

            return StreamingResponse(
                gen(), media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
            )
