"""Webhook 接入(与主 Web 同端口 8080)。

任务持久化于统一 SQLite(webhook_tasks 表)：状态/结果可查、进程重启后：
- 'pending' → 自动续跑(resume_pending)
- 遗留 'running'(崩溃残留, 超过 stale_after) → 标记 failed(中断)
配置沿用 config/plugins/webhook.json 的 tokens/path/callback_url 超时等字段。
"""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("web.webhook")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS webhook_tasks (
    task_id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    session_id TEXT DEFAULT '',
    callback_url TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    result TEXT,
    error TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_status ON webhook_tasks(status);
"""


@dataclass
class WebhookConfig:
    path: str = "/webhook/execute"
    tokens: list[str] = field(default_factory=list)
    callback_timeout: int = 300
    max_content_length: int = 10000
    stale_after_seconds: int = 600


@dataclass
class WebhookTask:
    task_id: str
    content: str
    session_id: str | None = None
    callback_url: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    status: str = "pending"
    result: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "content": self.content,
            "session_id": self.session_id or "", "callback_url": self.callback_url or "",
            "status": self.status, "result": self.result, "error": self.error,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


def _load_config() -> WebhookConfig:
    cfg = WebhookConfig()
    candidates = [
        os.path.join("config", "plugins", "webhook.json"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                     "config", "plugins", "webhook.json"),
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
                    stale_after_seconds=data.get("stale_after_seconds", 600),
                )
                logger.info(f"Webhook 配置加载自 {path}")
            except Exception as e:  # noqa: BLE001
                logger.error(f"Webhook 配置读取失败 {path}: {e}")
            break
    return cfg


class WebhookStore:
    """webhook_tasks 表的读写封装（复用主 storage 连接池）。"""

    @staticmethod
    def _conn():
        from storage.storage import get_storage
        return get_storage()

    @staticmethod
    def ensure():
        st = WebhookStore._conn()
        if not st:
            return
        with st.get_connection() as conn:
            conn.executescript(_CREATE_TABLE)
            conn.commit()

    @staticmethod
    def upsert(task: WebhookTask):
        st = WebhookStore._conn()
        if not st:
            return
        task.updated_at = datetime.now().isoformat()
        with st.get_connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO webhook_tasks
                   (task_id, content, session_id, callback_url, status, result, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task.task_id, task.content, task.session_id or "", task.callback_url or "",
                 task.status, task.result, task.error, task.created_at, task.updated_at),
            )
            conn.commit()

    @staticmethod
    def get(task_id: str) -> WebhookTask | None:
        st = WebhookStore._conn()
        if not st:
            return None
        with st.get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM webhook_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return WebhookStore._row_to_task(row) if row else None

    @staticmethod
    def list(status: str | None = None, limit: int = 50) -> list[WebhookTask]:
        st = WebhookStore._conn()
        if not st:
            return []
        sql = "SELECT * FROM webhook_tasks"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY rowid DESC LIMIT ?"
        args.append(min(limit, 200))
        with st.get_connection() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [WebhookStore._row_to_task(r) for r in rows]

    @staticmethod
    def _row_to_task(row) -> WebhookTask:
        return WebhookTask(
            task_id=row["task_id"], content=row["content"],
            session_id=row["session_id"] or None, callback_url=row["callback_url"] or None,
            status=row["status"], result=row["result"], error=row["error"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


class WebhookRuntime:
    """注册期返回的运行句柄：允许主服务在事件循环就绪后执行任务续跑。"""

    def __init__(self, cfg: WebhookConfig, run_one: Callable):
        self.cfg = cfg
        self._run_one = run_one

    async def resume_pending(self):
        WebhookStore.ensure()
        stale_after = getattr(self.cfg, "stale_after_seconds", 600)
        cut = datetime.now() - timedelta(seconds=stale_after)
        resumed = 0
        for task in WebhookStore.list():
            if task.status == "pending":
                task.status = "running"
                WebhookStore.upsert(task)
                asyncio.create_task(self._run_one(task))
                resumed += 1
            elif task.status == "running":
                # 崩溃残留：超过阈值视为中断，否则保留等待(可能仍在别的进程跑)
                try:
                    old = datetime.fromisoformat(task.updated_at)
                    if old < cut:
                        task.status = "failed"
                        task.error = "进程重启导致任务中断"
                        WebhookStore.upsert(task)
                except (ValueError, TypeError):
                    task.status = "failed"
                    task.error = "状态异常(进程重启)"
                    WebhookStore.upsert(task)
        if resumed:
            logger.info(f"[webhook] 重启续跑 {resumed} 个 pending 任务")


def register_webhook_routes(app: FastAPI, agent_provider: Callable[[], Any]) -> WebhookRuntime:
    cfg = _load_config()
    WebhookStore.ensure()

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
                "task_id": task.task_id, "status": task.status,
                "result": task.result, "error": task.error,
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

    async def run_one(task: WebhookTask) -> None:
        task.status = "running"
        WebhookStore.upsert(task)
        try:
            router = get_router()
            if router is None:
                raise RuntimeError("Agent not initialized")
            result = await router.route(
                task.content, channel="webhook",
                session_id=task.session_id,
                user_name="webhook",
            )
            task.status = "completed"
            task.result = result.result if hasattr(result, "result") else str(result)
            logger.info(f"Webhook task {task.task_id} completed")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            logger.error(f"Webhook task {task.task_id} failed: {e}")
        finally:
            WebhookStore.upsert(task)
        await send_callback(task)

    async def execute_sync(task: WebhookTask) -> Any:
        try:
            await asyncio.wait_for(run_one(task), timeout=cfg.callback_timeout)
            return {"task_id": task.task_id, "status": task.status,
                    "result": task.result, "error": task.error}
        except asyncio.TimeoutError:
            task.status = "failed"
            task.error = "Task timed out"
            WebhookStore.upsert(task)
            return JSONResponse({"task_id": task.task_id, "status": "failed",
                                 "error": "Task timed out"}, status_code=504)
        except Exception as e:  # noqa: BLE001
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            WebhookStore.upsert(task)
            return JSONResponse({"task_id": task.task_id, "status": "failed",
                                 "error": task.error}, status_code=500)

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
                    status_code=400)
            task_id = data.get("task_id") or str(uuid.uuid4())
            session_id = data.get("session_id") or f"webhook_{task_id[:8]}"
            task = WebhookTask(task_id=task_id, content=content, session_id=session_id,
                               callback_url=data.get("callback_url"))
            WebhookStore.upsert(task)

            if data.get("sync", False):
                return await execute_sync(task)

            asyncio.create_task(run_one(task))
            return {"task_id": task_id, "status": "pending",
                    "message": "Task submitted successfully",
                    "status_url": f"{cfg.path}/{task_id}"}
        except Exception as e:  # noqa: BLE001
            logger.error(f"Handle execute error: {e}")
            return JSONResponse({"error": str(e), "code": 500}, status_code=500)

    @app.get(cfg.path + "/{task_id}")
    async def task_status(task_id: str):
        task = WebhookStore.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found", "code": 404}, status_code=404)
        return {"task_id": task.task_id, "status": task.status,
                "created_at": task.created_at, "error": task.error}

    @app.get(cfg.path + "/{task_id}/result")
    async def task_result(task_id: str):
        task = WebhookStore.get(task_id)
        if not task:
            return JSONResponse({"error": "Task not found", "code": 404}, status_code=404)
        if task.status == "pending":
            return JSONResponse({"error": "Task not started", "code": 400}, status_code=400)
        if task.status == "running":
            return JSONResponse({"error": "Task still running", "code": 202}, status_code=202)
        return {"task_id": task.task_id, "status": task.status,
                "result": task.result, "error": task.error}

    @app.get("/webhook/tasks")
    async def list_tasks(status: str | None = None, limit: int = 50):
        tasks = WebhookStore.list(status=status, limit=limit)
        items = [{"task_id": t.task_id, "status": t.status, "created_at": t.created_at}
                 for t in tasks]
        return {"count": len(items), "tasks": items}

    @app.get("/webhook/health")
    async def health():
        return {"status": "ok", "service": "webhook", "path": cfg.path,
                "persist": "sqlite", "resume": True}

    runtime = WebhookRuntime(cfg, run_one)
    logger.info(f"Webhook 路由已注册(DB 持久化+重启续跑): {cfg.path} · "
                f"tokens={'启用' if cfg.tokens else '未配置(开放)'}")
    return runtime
