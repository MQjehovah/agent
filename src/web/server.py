import asyncio
import logging
import os
import threading
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI

logger = logging.getLogger("agent.web")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class ChatSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.created_at = datetime.now().isoformat()
        self.is_streaming = False
        self.messages: list[dict[str, Any]] = []
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
    """FastAPI Web 服务"""

    MAX_WEB_SESSIONS = 500
    _instance = None

    @classmethod
    def instance(cls):
        return cls._instance

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
        self._log_handler = None
        self._setup_routes()

    def set_agent(self, agent):
        self.agent = agent

    def set_kanban(self, kanban_board):
        self._kanban = kanban_board

    def _get_or_create_session(self, session_id: str) -> ChatSession:
        with self._session_lock:
            if session_id not in self._sessions:
                if len(self._sessions) >= self.MAX_WEB_SESSIONS:
                    oldest = min(self._sessions.values(), key=lambda s: s.created_at)
                    self._sessions.pop(oldest.session_id, None)
                self._sessions[session_id] = ChatSession(session_id)
            return self._sessions[session_id]

    def start(self):
        loop = self.loop or asyncio.get_event_loop()
        config = uvicorn.Config(
            self._app, host=self.host, port=self.port,
            log_level="warning", loop="asyncio", access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server.install_signal_handlers = lambda: None

        # 挂载日志流 handler
        from web.auth import LogStreamHandler
        self._log_handler = LogStreamHandler()
        self._log_handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s %(message)s"))
        self._log_handler.set_loop(loop)
        logging.getLogger("agent").addHandler(self._log_handler)
        self._ensure_admin_user()
        loop.create_task(self._server.serve())
        logger.info(f"Web UI (FastAPI/uvicorn) started at http://{self.host}:{self.port}")

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        logger.info("WebServer stopped")

    def _ensure_admin_user(self):
        from storage.storage import get_storage
        storage = get_storage()
        if not storage:
            return
        with storage.get_connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM rbac_users WHERE role = 'admin' AND password_hash != ''"
            ).fetchone()
            if row and row[0] > 0:
                return
            cur = conn.execute(
                "INSERT OR IGNORE INTO rbac_users (name, department, role, status, created_at) "
                "VALUES ('admin', '', 'admin', 'active', datetime('now'))"
            )
            user_id = cur.lastrowid
            if not user_id:
                user_id = conn.execute(
                    "SELECT id FROM rbac_users WHERE name = 'admin' AND role = 'admin'"
                ).fetchone()[0]
            conn.commit()
        storage.set_user_password(user_id, "admin123")
        logger.info("已创建默认 admin 用户 (admin/admin123)，请尽快修改密码")

    def _setup_routes(self):
        from web.auth import register_auth_routes
        from web.routes import register_api_routes
        register_auth_routes(self._app, self)
        register_api_routes(self._app, self)
