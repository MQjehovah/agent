import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.webhook")


@dataclass
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8081
    path: str = "/webhook/execute"
    tokens: list[str] = field(default_factory=list)
    callback_timeout: int = 30
    max_content_length: int = 10000


@dataclass
class WebhookTask:
    task_id: str
    content: str
    session_id: str | None = None
    callback_url: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "pending"
    result: str | None = None
    error: str | None = None


class WebhookPlugin(BasePlugin):
    """Webhook 插件（FastAPI/uvicorn）。

    与旧 Flask 版本的差异：服务运行在 agent 的 asyncio 事件循环内，
    路由直接 `await agent_executor(...)`，无需独立的事件循环线程与
    asyncio.run_coroutine_threadsafe 跨线程桥接。
    """

    name = "webhook"
    description = "Webhook插件，提供HTTP API接口执行任务"
    version = "1.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                self.config_dir or os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "plugins", "webhook.json"
            )

        self.config = WebhookConfig()

        if os.path.exists(config_file):
            try:
                with open(config_file, encoding="utf-8") as f:
                    data = json.load(f)

                self.config = WebhookConfig(
                    host=data.get("host", "0.0.0.0"),
                    port=data.get("port", 8081),
                    path=data.get("path", "/webhook/execute"),
                    tokens=data.get("tokens", []),
                    callback_timeout=data.get("callback_timeout", 30),
                    max_content_length=data.get("max_content_length", 10000)
                )
                logger.info(f"Loaded webhook config from {config_file}")
            except Exception as e:
                logger.error(f"Failed to load webhook config: {e}")

        self.tasks: dict[str, WebhookTask] = {}
        self.agent_executor: Callable | None = None
        self._server: uvicorn.Server | None = None
        self._serve_task: asyncio.Task | None = None
        self._app: FastAPI | None = None
        self._task_lock = asyncio.Lock()

    def _build_app(self) -> FastAPI:
        """构建 FastAPI 应用（供 start() 与测试复用）"""
        self._app = FastAPI(title="Agent Webhook")
        self._app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "X-Webhook-Token"],
            max_age=86400,
        )
        self._setup_routes()
        return self._app

    def start(self):
        self._build_app()
        self._silence_lifespan_errors()

        config = uvicorn.Config(
            self._app, host=self.config.host, port=self.config.port,
            log_level="warning", loop="asyncio", access_log=False,
        )
        self._server = uvicorn.Server(config)
        # 嵌入式运行：禁用 uvicorn 自带的信号处理，统一由主程序控制
        self._server.install_signal_handlers = lambda: None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        async def _safe_serve():
            with contextlib.suppress(asyncio.CancelledError):
                await self._server.serve()
        self._serve_task = loop.create_task(_safe_serve())
        logger.info(f"Webhook (FastAPI/uvicorn) started at http://{self.config.host}:{self.config.port}")

    def _silence_lifespan_errors(self):
        """Filter out CancelledError traceback from uvicorn's lifespan handler."""
        uvicorn_logger = logging.getLogger("uvicorn.error")
        class _Filter(logging.Filter):
            def filter(self, record):
                msg = record.getMessage()
                return "CancelledError" not in msg and "lifespan" not in msg
        uvicorn_logger.addFilter(_Filter())

    def _setup_routes(self):
        webhook_path = self.config.path

        @self._app.post(webhook_path)
        async def execute(request: Request):
            return await self._handle_execute(request)

        @self._app.get(f"{webhook_path}/{{task_id}}")
        async def get_task_status(task_id: str):
            return await self._handle_get_status(task_id)

        @self._app.get(f"{webhook_path}/{{task_id}}/result")
        async def get_task_result(task_id: str):
            return await self._handle_get_result(task_id)

        @self._app.get("/webhook/tasks")
        async def list_tasks(status: str | None = Query(None), limit: int = Query(50)):
            return await self._handle_list_tasks(status, limit)

        @self._app.get("/health")
        async def health():
            return {"status": "ok", "service": "webhook"}

    def _validate_token(self, request: Request) -> bool:
        if not self.config.tokens:
            return True

        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else request.headers.get("X-Webhook-Token", "")

        return token in self.config.tokens

    async def _handle_execute(self, request: Request):
        if not self._validate_token(request):
            return JSONResponse({"error": "Unauthorized", "code": 401}, status_code=401)

        try:
            data = await request.json()
            if not data:
                return JSONResponse({"error": "Invalid JSON body", "code": 400}, status_code=400)

            content = data.get("content") or data.get("task") or data.get("prompt")
            if not content:
                return JSONResponse({"error": "Missing 'content' field", "code": 400}, status_code=400)

            if len(content) > self.config.max_content_length:
                return JSONResponse({
                    "error": f"Content too long, max {self.config.max_content_length} characters",
                    "code": 400
                }, status_code=400)

            task_id = data.get("task_id") or str(uuid.uuid4())
            session_id = data.get("session_id") or f"webhook_{task_id[:8]}"
            callback_url = data.get("callback_url")
            sync = data.get("sync", False)

            task = WebhookTask(
                task_id=task_id,
                content=content,
                session_id=session_id,
                callback_url=callback_url
            )
            async with self._task_lock:
                self.tasks[task_id] = task

            router = getattr(self.plugin_manager, "router", None) if self.plugin_manager else None
            if not router and not self.agent_executor:
                task.status = "failed"
                task.error = "Router not registered"
                return JSONResponse({"error": "Router not registered", "code": 500}, status_code=500)

            if sync:
                return await self._execute_sync(task)

            # 异步模式：后台执行，立即返回
            asyncio.create_task(self._run_task_and_callback(task))
            return {
                "task_id": task_id,
                "status": "pending",
                "message": "Task submitted successfully",
                "status_url": f"{self.config.path}/{task_id}"
            }

        except Exception as e:
            logger.error(f"Handle execute error: {e}")
            return JSONResponse({"error": str(e), "code": 500}, status_code=500)

    async def _execute_sync(self, task: WebhookTask):
        """同步执行任务（等待完成，带超时）"""
        async with self._task_lock:
            task.status = "running"
        try:
            await asyncio.wait_for(
                self._run_task_and_callback(task),
                timeout=self.config.callback_timeout,
            )
            return {
                "task_id": task.task_id,
                "status": task.status,
                "result": task.result,
                "error": task.error,
            }
        except asyncio.TimeoutError:
            async with self._task_lock:
                task.status = "failed"
                task.error = "Task timed out"
            logger.error(f"Task {task.task_id} timed out after {self.config.callback_timeout}s")
            return JSONResponse({
                "task_id": task.task_id,
                "status": "failed",
                "error": "Task timed out"
            }, status_code=504)
        except Exception as e:
            async with self._task_lock:
                task.status = "failed"
                task.error = f"{type(e).__name__}: {e}"
            logger.error(f"Task {task.task_id} failed: {e}")
            return JSONResponse({
                "task_id": task.task_id,
                "status": "failed",
                "error": task.error
            }, status_code=500)

    async def _run_task_and_callback(self, task: WebhookTask):
        """执行单个任务并在结束后回调"""
        async with self._task_lock:
            task.status = "running"

        try:
            router = getattr(self.plugin_manager, "router", None) if self.plugin_manager else None
            if router:
                uid = task.session_id or "task"
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                router.on_response("webhook", uid, future.set_result)
                router.publish(task.content, channel="webhook", user_id=uid)
                result = await future
                result_str = result.result if hasattr(result, "result") else str(result)
            elif self.agent_executor:
                result = await self.agent_executor(task.session_id, task.content)
                result_str = result.result if hasattr(result, "result") else str(result)
            else:
                result_str = "router not registered"

            async with self._task_lock:
                task.status = "completed"
                task.result = result_str

            logger.info(f"Task {task.task_id} completed")
            await self._send_callback(task)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            async with self._task_lock:
                task.status = "failed"
                task.error = f"{type(e).__name__}: {e}"

            logger.error(f"Task {task.task_id} failed: {e}")
            await self._send_callback(task)

    async def _send_callback(self, task: WebhookTask):
        if not task.callback_url:
            return

        try:
            import httpx
            payload = {
                "task_id": task.task_id,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "completed_at": datetime.now().isoformat()
            }

            headers = {"Content-Type": "application/json; charset=utf-8"}
            if self.config.tokens:
                headers["X-Webhook-Token"] = self.config.tokens[0]

            async with httpx.AsyncClient(timeout=self.config.callback_timeout) as client:
                response = await client.post(task.callback_url, json=payload, headers=headers)
                logger.info(f"Callback sent to {task.callback_url}, status: {response.status_code}")

        except Exception as e:
            logger.error(f"Callback failed for task {task.task_id}: {e}")

    async def _handle_get_status(self, task_id: str):
        async with self._task_lock:
            task = self.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found", "code": 404}, status_code=404)
        return {
            "task_id": task.task_id,
            "status": task.status,
            "created_at": task.created_at,
            "error": task.error
        }

    async def _handle_get_result(self, task_id: str):
        async with self._task_lock:
            task = self.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found", "code": 404}, status_code=404)

        if task.status == "pending":
            return JSONResponse({"error": "Task not started", "code": 400}, status_code=400)
        if task.status == "running":
            return JSONResponse({"error": "Task still running", "code": 202}, status_code=202)

        return {
            "task_id": task.task_id,
            "status": task.status,
            "result": task.result,
            "error": task.error
        }

    async def _handle_list_tasks(self, status_filter: str | None, limit: int):
        limit = min(limit, 100)
        async with self._task_lock:
            tasks_snapshot = list(self.tasks.values())

        tasks = []
        for task in tasks_snapshot[-limit:]:
            if status_filter and task.status != status_filter:
                continue
            tasks.append({
                "task_id": task.task_id,
                "status": task.status,
                "created_at": task.created_at
            })

        return {"count": len(tasks), "tasks": tasks}

    def stop(self):
        """停止 uvicorn 服务"""
        if self._server is not None:
            self._server.should_exit = True
        if self._serve_task is not None and not self._serve_task.done():
            self._serve_task.cancel()
        logger.info("Webhook plugin stopped")

    def get_stats(self) -> dict[str, Any]:
        tasks_values = list(self.tasks.values())
        return {
            "total_tasks": len(tasks_values),
            "pending": sum(1 for t in tasks_values if t.status == "pending"),
            "running": sum(1 for t in tasks_values if t.status == "running"),
            "completed": sum(1 for t in tasks_values if t.status == "completed"),
            "failed": sum(1 for t in tasks_values if t.status == "failed")
        }


plugin = WebhookPlugin
