"""Webhook 接入(原独立 8081 插件的承接实现,单端口 8080)。

外部系统(工单 / 自动化)通过 /webhook/execute 提交异步任务,
支持 status / result 轮询与 callback_url 回调。配置沿用
config/plugins/webhook.json 的 tokens / path / callback_url 超时等字段
(port / standalone 字段不再使用 —— 服务与主 Web 同端口)。
"""

import asyncio
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("web.webhook")


@dataclass
class WebhookConfig:
    path: str = "/webhook/execute"
    tokens: list[str] = field(default_factory=list)
    callback_timeout: int = 300
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


def _load_config() -> WebhookConfig:
    """兼容读取原插件配置(config/plugins/webhook.json);忽略 port/standalone。"""
    import json

    cfg = WebhookConfig()
    candidates = [
        os.path.join("config", "plugins", "webhook.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config", "plugins", "webhook.json"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                cfg = WebhookConfig(
                    path=data.get("path", cfg.path),
                    tokens=data.get("tokens", []),
                    callback_timeout=data.get("callback_timeout", 300),
                    max_content_length=data.get("max_content_length", 10000),
                )
                logger.info(f"Webhook 配置加载自 {path}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"Webhook 配置读取失败 {path}: {e}")
            break
    return cfg


def register_webhook_routes(app: FastAPI, agent_provider: Callable[[], Any]) -> None:
    """把 webhook 路由注册到主 Web 应用(与 UI/API 共享 8080)。"""
    cfg = _load_config()
    tasks: dict[str, WebhookTask] = {}
    task_lock = asyncio.Lock()

    def get_router():
        from channels import MessageRouter

        agent = agent_provider()
        return MessageRouter(agent) if agent else None

    def validate_token(request: Request) -> bool:
        if not cfg.tokens:
            return True
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else request.headers.get("X-Webhook-Token", "")
        return token in cfg.tokens

    async def send_callback(task: WebhookTask) -> None:
        if not task.callback_url:
            return
        try:
            import httpx

            payload = {
                "task_id": task.task_id,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "completed_at": datetime.now().isoformat(),
            }
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if cfg.tokens:
                headers["X-Webhook-Token"] = cfg.tokens[0]
            async with httpx.AsyncClient(timeout=min(cfg.callback_timeout, 60)) as client:
                response = await client.post(task.callback_url, json=payload, headers=headers)
                logger.info(f"Callback sent to {task.callback_url}, status: {response.status_code}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Callback failed for task {task.task_id}: {e}")

    async def run_task_and_callback(task: WebhookTask) -> None:
        async with task_lock:
            task.status = "running"
        try:
            router = get_router()
            if router is None:
                raise RuntimeError("Agent not initialized")
            result = await router.route(
                task.content, channel="webhook",
                session_id=task.session_id,
                user_name="webhook",
            )
            result_str = result.result if hasattr(result, "result") else str(result)
            async with task_lock:
                task.status = "completed"
                task.result = result_str
            logger.info(f"Webhook task {task.task_id} completed")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            async with task_lock:
                task.status = "failed"
                task.error = f"{type(e).__name__}: {e}"
            logger.error(f"Webhook task {task.task_id} failed: {e}")
        await send_callback(task)

    async def execute_sync(task: WebhookTask) -> Any:
        async with task_lock:
            task.status = "running"
        try:
            await asyncio.wait_for(run_task_and_callback(task), timeout=cfg.callback_timeout)
            return {
                "task_id": task.task_id,
                "status": task.status,
                "result": task.result,
                "error": task.error,
            }
        except asyncio.TimeoutError:
            async with task_lock:
                task.status = "failed"
                task.error = "Task timed out"
            logger.error(f"Task {task.task_id} timed out after {cfg.callback_timeout}s")
            return JSONResponse({"task_id": task.task_id, "status": "failed", "error": "Task timed out"}, status_code=504)
        except Exception as e:  # noqa: BLE001
            async with task_lock:
                task.status = "failed"
                task.error = f"{type(e).__name__}: {e}"
            return JSONResponse({"task_id": task.task_id, "status": "failed", "error": task.error}, status_code=500)

    @app.post(cfg.path)
    async def execute(request: Request):
        if not validate_token(request):
            return JSONResponse({"error": "Unauthorized", "code": 401}, status_code=401)
        try:
            data = await request.json()
            if not data:
                return JSONResponse({"error": "Invalid JSON body", "code": 400}, status_code=400)
            content = data.get("content") or data.get("task") or data.get("prompt")
            if not content:
                return JSONResponse({"error": "Missing 'content' field", "code": 400}, status_code=400)
            if len(content) > cfg.max_content_length:
                return JSONResponse(
                    {"error": f"Content too long, max {cfg.max_content_length} characters", "code": 400},
                    status_code=400,
                )

            task_id = data.get("task_id") or str(uuid.uuid4())
            session_id = data.get("session_id") or f"webhook_{task_id[:8]}"
            task = WebhookTask(
                task_id=task_id,
                content=content,
                session_id=session_id,
                callback_url=data.get("callback_url"),
            )
            async with task_lock:
                tasks[task_id] = task

            if data.get("sync", False):
                return await execute_sync(task)

            asyncio.create_task(run_task_and_callback(task))
            return {
                "task_id": task_id,
                "status": "pending",
                "message": "Task submitted successfully",
                "status_url": f"{cfg.path}/{task_id}",
            }
        except Exception as e:  # noqa: BLE001
            logger.error(f"Handle execute error: {e}")
            return JSONResponse({"error": str(e), "code": 500}, status_code=500)

    @app.get(cfg.path + "/{task_id}")
    async def task_status(task_id: str):
        async with task_lock:
            task = tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found", "code": 404}, status_code=404)
        return {"task_id": task.task_id, "status": task.status, "created_at": task.created_at, "error": task.error}

    @app.get(cfg.path + "/{task_id}/result")
    async def task_result(task_id: str):
        async with task_lock:
            task = tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found", "code": 404}, status_code=404)
        if task.status == "pending":
            return JSONResponse({"error": "Task not started", "code": 400}, status_code=400)
        if task.status == "running":
            return JSONResponse({"error": "Task still running", "code": 202}, status_code=202)
        return {"task_id": task.task_id, "status": task.status, "result": task.result, "error": task.error}

    @app.get("/webhook/tasks")
    async def list_tasks(status: str | None = None, limit: int = 50):
        limit = min(limit, 100)
        async with task_lock:
            snapshot = list(tasks.values())
        items = [
            {"task_id": t.task_id, "status": t.status, "created_at": t.created_at}
            for t in snapshot[-limit:]
            if not status or t.status == status
        ]
        return {"count": len(items), "tasks": items}

    @app.get("/webhook/health")
    async def health():
        return {"status": "ok", "service": "webhook", "path": cfg.path}

    logger.info(f"Webhook 路由已注册(与主 Web 同端口):{cfg.path} · tokens={'启用' if cfg.tokens else '未配置(开放)'}")
