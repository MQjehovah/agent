import os
import json
import uuid
import logging
import threading
import asyncio
import queue
import time
from typing import Optional, Dict, Any, List
from datetime import datetime

logger = logging.getLogger("agent.web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ChatSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.created_at = datetime.now().isoformat()
        self.token_queue: queue.Queue = queue.Queue()
        self.is_streaming = False
        self.messages: List[Dict[str, Any]] = []


class WebServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self.agent = None
        self.panel = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_ready = threading.Event()
        self._app = None
        self._thread: Optional[threading.Thread] = None
        self._sessions: Dict[str, ChatSession] = {}
        self._session_lock = threading.Lock()

    def start_event_loop(self):
        self._loop_thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._loop_thread.start()
        self._loop_ready.wait(timeout=10)
        logger.info("WebServer asyncio event loop started")

    def _run_event_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._loop_ready.set()
        self.loop.run_forever()

    def set_agent(self, agent):
        self.agent = agent

    def set_panel(self, panel):
        self.panel = panel

    def start(self):
        try:
            from flask import Flask, request, Response, send_from_directory
        except ImportError:
            logger.error("flask is required. Install: pip install flask")
            return

        self.start_event_loop()
        logging.getLogger("werkzeug").setLevel(logging.WARNING)

        self._app = Flask(__name__, static_folder=None)
        self._setup_routes()
        self._thread = threading.Thread(
            target=self._run_server, args=(self.host, self.port), daemon=True
        )
        self._thread.start()
        logger.info(f"Web UI started at http://{self.host}:{self.port}")

    def _setup_routes(self):
        from flask import request, Response, send_from_directory, abort

        @self._app.route("/")
        def index():
            return send_from_directory(STATIC_DIR, "index.html")

        @self._app.route("/static/<path:filename>")
        def static_files(filename):
            return send_from_directory(STATIC_DIR, filename)

        @self._app.route("/api/agent/status", methods=["GET"])
        def agent_status():
            if not self.agent:
                return self._json({"error": "Agent not initialized"}, 503)
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
            if self.panel:
                panel_stats = self.panel.get_stats()

            return self._json({
                "name": self.agent.name or "Agent",
                "description": self.agent.description or "",
                "status": self.agent.status,
                "model": self.agent.client.model if self.agent.client else "",
                "tasks": status_counts,
                "usage": usage,
                "tools": self.agent.tool_registry.list_tools() if self.agent.tool_registry else [],
                "subagents": subagents,
                "panel": panel_stats,
            })

        @self._app.route("/api/chat", methods=["POST"])
        def chat():
            if not self.agent:
                return self._json({"error": "Agent not initialized"}, 503)
            data = request.get_json()
            if not data or not data.get("message"):
                return self._json({"error": "Missing message"}, 400)

            message = data["message"].strip()
            if not message:
                return self._json({"error": "Empty message"}, 400)

            session_id = data.get("session_id")
            if not session_id:
                session_id = f"web_{uuid.uuid4().hex[:8]}"

            with self._session_lock:
                if session_id not in self._sessions:
                    self._sessions[session_id] = ChatSession(session_id)

            chat_session = self._sessions[session_id]
            chat_session.messages.append({"role": "user", "content": message, "time": datetime.now().isoformat()})
            chat_session.is_streaming = True

            return self._json({"session_id": session_id, "status": "processing"})

        @self._app.route("/api/chat/stream", methods=["POST"])
        def chat_stream():
            if not self.agent:
                return self._json({"error": "Agent not initialized"}, 503)
            data = request.get_json()
            if not data or not data.get("message"):
                return self._json({"error": "Missing message"}, 400)

            message = data["message"].strip()
            session_id = data.get("session_id", f"web_{uuid.uuid4().hex[:8]}")

            with self._session_lock:
                if session_id not in self._sessions:
                    self._sessions[session_id] = ChatSession(session_id)
                chat_session = self._sessions[session_id]

            chat_session.messages.append({"role": "user", "content": message, "time": datetime.now().isoformat()})
            chat_session.is_streaming = True
            chat_session.token_queue = queue.Queue()

            def generate():
                token_queue = chat_session.token_queue
                agent_ref = self.agent
                loop_ref = self.loop

                async def run_agent():
                    original_on_token = agent_ref.on_token
                    full_response = []

                    async def on_token(token):
                        full_response.append(token)
                        token_queue.put(("token", token))

                    agent_ref.on_token = on_token
                    try:
                        result = await agent_ref.run(message, session_id=session_id)
                        resp_text = result.result if result and hasattr(result, 'result') else ""
                        if full_response:
                            resp_text = "".join(full_response)
                        token_queue.put(("done", resp_text))
                    except Exception as e:
                        token_queue.put(("error", str(e)))
                    finally:
                        agent_ref.on_token = original_on_token
                        chat_session.is_streaming = False

                future = asyncio.run_coroutine_threadsafe(run_agent(), loop_ref)

                try:
                    while True:
                        try:
                            event_type, content = token_queue.get(timeout=1.0)
                            if event_type == "token":
                                yield f"data: {json.dumps({'type': 'token', 'content': content}, ensure_ascii=False)}\n\n"
                            elif event_type == "done":
                                chat_session.messages.append({
                                    "role": "assistant", "content": content,
                                    "time": datetime.now().isoformat()
                                })
                                yield f"data: {json.dumps({'type': 'done', 'content': content}, ensure_ascii=False)}\n\n"
                                break
                            elif event_type == "error":
                                chat_session.messages.append({
                                    "role": "assistant", "content": f"Error: {content}",
                                    "time": datetime.now().isoformat()
                                })
                                yield f"data: {json.dumps({'type': 'error', 'content': content}, ensure_ascii=False)}\n\n"
                                break
                        except queue.Empty:
                            if future.done():
                                try:
                                    future.result()
                                except Exception as e:
                                    yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
                                break
                            yield f"data: {json.dumps({'type': 'heartbeat'}, ensure_ascii=False)}\n\n"
                except GeneratorExit:
                    pass
                finally:
                    chat_session.is_streaming = False

            return Response(generate(), mimetype="text/event-stream", headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            })

        @self._app.route("/api/tasks", methods=["GET"])
        def list_tasks():
            if not self.agent or not self.agent.task_manager:
                return self._json({"tasks": []})
            tasks = self.agent.task_manager.list_tasks()
            return self._json({"tasks": tasks, "count": len(tasks)})

        @self._app.route("/api/tasks/<task_id>", methods=["GET"])
        def get_task(task_id):
            if not self.agent or not self.agent.task_manager:
                return self._json({"error": "Not found"}, 404)
            task = self.agent.task_manager.get_task(task_id)
            if not task:
                return self._json({"error": "Task not found"}, 404)
            return self._json({
                "id": task.id, "description": task.description,
                "status": task.status, "created_at": task.created_at,
                "result": task.result, "error": task.error,
            })

        @self._app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
        def cancel_task(task_id):
            if not self.agent or not self.agent.task_manager:
                return self._json({"error": "Not found"}, 404)
            future = asyncio.run_coroutine_threadsafe(
                self.agent.task_manager.cancel_task(task_id), self.loop
            )
            success = future.result(timeout=5)
            return self._json({"success": success, "task_id": task_id})

        @self._app.route("/api/sessions", methods=["GET"])
        def list_sessions():
            with self._session_lock:
                sessions = []
                for sid, s in self._sessions.items():
                    sessions.append({
                        "id": sid, "created_at": s.created_at,
                        "message_count": len(s.messages),
                        "is_streaming": s.is_streaming,
                    })
            return self._json({"sessions": sessions})

        @self._app.route("/api/sessions/<session_id>/messages", methods=["GET"])
        def session_messages(session_id):
            with self._session_lock:
                chat_session = self._sessions.get(session_id)
            if not chat_session:
                return self._json({"error": "Session not found"}, 404)
            return self._json({"messages": chat_session.messages})

        @self._app.route("/api/sessions/<session_id>", methods=["DELETE"])
        def delete_session(session_id):
            with self._session_lock:
                self._sessions.pop(session_id, None)
            return self._json({"success": True})

        # 任务面板 API
        @self._app.route("/api/panel", methods=["GET"])
        def panel_list():
            if not self.panel:
                return self._json({"error": "Panel not available", "code": 503}, 503)
            try:
                tasks = self.panel.list_all()
                stats = self.panel.get_stats()
                return self._json({"stats": stats, "tasks": [t.to_dict() for t in tasks]})
            except Exception as e:
                logger.exception("Panel API error")
                return self._json({"error": str(e), "code": 500}, 500)

        @self._app.route("/api/panel", methods=["POST"])
        def panel_add():
            if not self.panel:
                return self._json({"error": "Panel not available"}, 503)
            data = request.get_json()
            if not data or "title" not in data:
                return self._json({"error": "Missing title"}, 400)
            task = self.panel.add_task(
                title=data["title"],
                description=data.get("description", ""),
                priority=data.get("priority", 3),
                interval=data.get("interval"),
                source="user",
            )
            return self._json({"task": task.to_dict()})

        @self._app.route("/api/panel/<task_id>", methods=["DELETE"])
        def panel_remove(task_id):
            if not self.panel:
                return self._json({"error": "Panel not available"}, 503)
            if self.panel.remove_task(task_id):
                return self._json({"success": True})
            return self._json({"error": "Task not found"}, 404)

        # Todo API
        @self._app.route("/api/todos", methods=["GET"])
        def todo_list():
            if not self.agent or not self.agent.tool_registry:
                return self._json({"error": "Agent not initialized"}, 503)
            todo_tool = self.agent.tool_registry.get_tool("todowrite")
            if not todo_tool:
                return self._json({"todos": []})
            data = todo_tool.get_todos("all")
            return self._json({"todos": data, "count": len(data)})

    def _json(self, data: Any, status: int = 200) -> "Response":
        from flask import Response
        return Response(
            json.dumps(data, ensure_ascii=False),
            status=status,
            mimetype="application/json; charset=utf-8",
        )

    def _run_server(self, host: str, port: int):
        self._app.run(host=host, port=port, threaded=True, use_reloader=False)

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=5)
        logger.info("WebServer stopped")
