import asyncio
import json
import logging
import threading
import uuid
import time
import concurrent.futures
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field
from datetime import datetime

try:
    from flask import Flask, request, Response
except ImportError:
    Flask = None

logger = logging.getLogger("webhook.plugin")


def json_response(data: Any, status: int = 200) -> Response:
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        mimetype='application/json; charset=utf-8'
    )


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


class WebhookPlugin:
    def __init__(self, config_path: Optional[str] = None):
        if Flask is None:
            raise ImportError("flask is required. Install: pip install flask")
        
        from .config import WebhookConfig
        self.config = WebhookConfig.load(config_path)
        self.tasks: Dict[str, WebhookTask] = {}
        self.agent_executor: Optional[Callable] = None
        self._app = Flask(__name__)
        self._thread: Optional[threading.Thread] = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=10)
        self._setup_routes()
    
    def _setup_routes(self):
        webhook_path = self.config.path
        
        @self._app.route(webhook_path, methods=["POST"])
        def execute():
            return self._handle_execute()
        
        @self._app.route(f"{webhook_path}/<task_id>", methods=["GET"])
        def get_task_status(task_id):
            return self._handle_get_status(task_id)
        
        @self._app.route(f"{webhook_path}/<task_id>/result", methods=["GET"])
        def get_task_result(task_id):
            return self._handle_get_result(task_id)
        
        @self._app.route("/webhook/tasks", methods=["GET"])
        def list_tasks():
            return self._handle_list_tasks()
        
        @self._app.route("/health", methods=["GET"])
        def health():
            return json_response({"status": "ok", "service": "webhook"})
    
    def register_agent(self, executor: Callable):
        self.agent_executor = executor
        logger.info("Agent executor registered")
    
    def _validate_token(self) -> bool:
        if not self.config.tokens:
            return True
        
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
        else:
            token = request.headers.get("X-Webhook-Token", "")
        
        return token in self.config.tokens
    
    def _handle_execute(self):
        if not self._validate_token():
            return json_response({"error": "Unauthorized", "code": 401}, 401)
        
        try:
            data = request.get_json()
            if not data:
                return json_response({"error": "Invalid JSON body", "code": 400}, 400)
            
            content = data.get("content") or data.get("task") or data.get("prompt")
            if not content:
                return json_response({"error": "Missing 'content' field", "code": 400}, 400)
            
            if len(content) > self.config.max_content_length:
                return json_response({
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
                return json_response({"error": "Agent not registered", "code": 500}, 500)
            
            if sync:
                return self._execute_sync(task)
            else:
                self._executor.submit(self._run_async_task, task)
                return json_response({
                    "task_id": task_id,
                    "status": "pending",
                    "message": "Task submitted successfully",
                    "status_url": f"{self.config.path}/{task_id}"
                })
        
        except Exception as e:
            logger.error(f"Handle execute error: {e}")
            return json_response({"error": str(e), "code": 500}, 500)
    
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
            
            return json_response({
                "task_id": task.task_id,
                "status": "completed",
                "result": result
            })
        except Exception as e:
            import traceback
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            logger.error(f"Task {task.task_id} failed: {e}\n{traceback.format_exc()}")
            return json_response({
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
            import traceback
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            logger.error(f"Task {task.task_id} failed: {e}\n{traceback.format_exc()}")
            
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
            return json_response({"error": "Task not found", "code": 404}, 404)
        
        return json_response({
            "task_id": task.task_id,
            "status": task.status,
            "created_at": task.created_at,
            "error": task.error
        })
    
    def _handle_get_result(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task:
            return json_response({"error": "Task not found", "code": 404}, 404)
        
        if task.status == "pending":
            return json_response({"error": "Task not started", "code": 400}, 400)
        
        if task.status == "running":
            return json_response({"error": "Task still running", "code": 202}, 202)
        
        return json_response({
            "task_id": task.task_id,
            "status": task.status,
            "result": task.result,
            "error": task.error
        })
    
    def _handle_list_tasks(self):
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
        
        return json_response({
            "count": len(tasks),
            "tasks": tasks
        })
    
    def start(self):
        host = self.config.host
        port = self.config.port
        
        self._thread = threading.Thread(
            target=self._run_server,
            args=(host, port),
            daemon=True
        )
        self._thread.start()
        logger.info(f"Webhook plugin started: http://{host}:{port}{self.config.path}")
    
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