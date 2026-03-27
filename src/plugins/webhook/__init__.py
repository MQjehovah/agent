import os
import json
import logging
import threading
import asyncio
import concurrent.futures
import uuid
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from datetime import datetime

from plugins.base import BasePlugin

logger = logging.getLogger("plugin.webhook")


@dataclass
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8081
    path: str = "/webhook/execute"
    tokens: List[str] = field(default_factory=list)
    callback_timeout: int = 30
    max_content_length: int = 10000


@dataclass
class WebhookTask:
    task_id: str
    content: str
    session_id: Optional[str] = None
    callback_url: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "pending"
    result: Optional[str] = None
    error: Optional[str] = None


class WebhookPlugin(BasePlugin):
    name = "webhook"
    description = "Webhook插件，提供HTTP API接口执行任务"
    version = "1.0.0"

    def _load_config(self):
        config_file = self.config_path
        if not config_file:
            config_file = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "config", "webhook.json"
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
        
        self.tasks: Dict[str, WebhookTask] = {}
        self._thread: Optional[threading.Thread] = None
        self._app = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)

    def start(self):
        try:
            from flask import Flask, request, Response
        except ImportError:
            logger.error("flask is required. Install: pip install flask")
            return
        
        self._app = Flask(__name__)
        self._setup_routes()
        
        host = self.config.host
        port = self.config.port
        
        self._thread = threading.Thread(
            target=self._run_server,
            args=(host, port),
            daemon=True
        )
        self._thread.start()

    def _setup_routes(self):
        from flask import request, Response
        webhook_path = self.config.path

        @self._app.route(webhook_path, methods=["POST"])
        def execute():
            return self._handle_execute(request)

        @self._app.route(f"{webhook_path}/<task_id>", methods=["GET"])
        def get_task_status(task_id):
            return self._handle_get_status(task_id)

        @self._app.route(f"{webhook_path}/<task_id>/result", methods=["GET"])
        def get_task_result(task_id):
            return self._handle_get_result(task_id)

        @self._app.route("/webhook/tasks", methods=["GET"])
        def list_tasks():
            return self._handle_list_tasks(request)

        @self._app.route("/health", methods=["GET"])
        def health():
            return self._json_response({"status": "ok", "service": "webhook"})

    def _json_response(self, data: Any, status: int = 200) -> "Response":
        from flask import Response
        return Response(
            json.dumps(data, ensure_ascii=False),
            status=status,
            mimetype='application/json; charset=utf-8'
        )

    def _validate_token(self, request) -> bool:
        if not self.config.tokens:
            return True
        
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = request.headers.get("X-Webhook-Token", "")
        
        return token in self.config.tokens

    def _handle_execute(self, request):
        if not self._validate_token(request):
            return self._json_response({"error": "Unauthorized", "code": 401}, 401)
        
        try:
            data = request.get_json()
            if not data:
                return self._json_response({"error": "Invalid JSON body", "code": 400}, 400)
            
            content = data.get("content") or data.get("task") or data.get("prompt")
            if not content:
                return self._json_response({"error": "Missing 'content' field", "code": 400}, 400)
            
            if len(content) > self.config.max_content_length:
                return self._json_response({
                    "error": f"Content too long, max {self.config.max_content_length} characters",
                    "code": 400
                }, 400)
            
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
            self.tasks[task_id] = task
            
            if not self.agent_executor:
                task.status = "failed"
                task.error = "Agent not registered"
                return self._json_response({"error": "Agent not registered", "code": 500}, 500)
            
            if sync:
                return self._execute_sync(task)
            else:
                self._executor.submit(self._run_async_task, task)
                return self._json_response({
                    "task_id": task_id,
                    "status": "pending",
                    "message": "Task submitted successfully",
                    "status_url": f"{self.config.path}/{task_id}"
                })
        
        except Exception as e:
            logger.error(f"Handle execute error: {e}")
            return self._json_response({"error": str(e), "code": 500}, 500)

    def _execute_sync(self, task: WebhookTask):
        task.status = "running"
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(
                self.agent_executor(task.session_id, task.content)
            )
            loop.close()
            
            task.status = "completed"
            task.result = result
            
            if task.callback_url:
                self._send_callback(task)
            
            return self._json_response({
                "task_id": task.task_id,
                "status": "completed",
                "result": result
            })
        except Exception as e:
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            logger.error(f"Task {task.task_id} failed: {e}")
            return self._json_response({
                "task_id": task.task_id,
                "status": "failed",
                "error": task.error
            }, 500)

    def _run_async_task(self, task: WebhookTask):
        task.status = "running"
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    self.agent_executor(task.session_id, task.content)
                )
                task.status = "completed"
                task.result = result
                logger.info(f"Task {task.task_id} completed")
                
                if task.callback_url:
                    self._send_callback(task)
            finally:
                loop.close()
        except Exception as e:
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            logger.error(f"Task {task.task_id} failed: {e}")
            
            if task.callback_url:
                self._send_callback(task)

    def _send_callback(self, task: WebhookTask):
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
            
            with httpx.Client(timeout=self.config.callback_timeout) as client:
                response = client.post(task.callback_url, json=payload, headers=headers)
                logger.info(f"Callback sent to {task.callback_url}, status: {response.status_code}")
        
        except Exception as e:
            logger.error(f"Callback failed for task {task.task_id}: {e}")

    def _handle_get_status(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return self._json_response({"error": "Task not found", "code": 404}, 404)
        
        return self._json_response({
            "task_id": task.task_id,
            "status": task.status,
            "created_at": task.created_at,
            "error": task.error
        })

    def _handle_get_result(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return self._json_response({"error": "Task not found", "code": 404}, 404)
        
        if task.status == "pending":
            return self._json_response({"error": "Task not started", "code": 400}, 400)
        
        if task.status == "running":
            return self._json_response({"error": "Task still running", "code": 202}, 202)
        
        return self._json_response({
            "task_id": task.task_id,
            "status": task.status,
            "result": task.result,
            "error": task.error
        })

    def _handle_list_tasks(self, request):
        status_filter = request.args.get("status")
        limit = min(int(request.args.get("limit", 50)), 100)
        
        tasks = []
        for task in list(self.tasks.values())[-limit:]:
            if status_filter and task.status != status_filter:
                continue
            tasks.append({
                "task_id": task.task_id,
                "status": task.status,
                "created_at": task.created_at
            })
        
        return self._json_response({
            "count": len(tasks),
            "tasks": tasks
        })

    def _run_server(self, host: str, port: int):
        self._app.run(host=host, port=port, threaded=True, use_reloader=False)

    def stop(self):
        logger.info("Webhook plugin stopped")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "total_tasks": len(self.tasks),
            "pending": sum(1 for t in self.tasks.values() if t.status == "pending"),
            "running": sum(1 for t in self.tasks.values() if t.status == "running"),
            "completed": sum(1 for t in self.tasks.values() if t.status == "completed"),
            "failed": sum(1 for t in self.tasks.values() if t.status == "failed")
        }


plugin = WebhookPlugin