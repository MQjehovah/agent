import asyncio
import json
import uuid
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Callable, Coroutine
from dataclasses import dataclass, field

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


@dataclass
class BackgroundTask:
    id: str
    description: str
    status: str = "pending"  # pending / running / completed / failed / cancelled
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    result: Optional[str] = None
    error: Optional[str] = None
    _async_task: Optional[asyncio.Task] = field(default=None, repr=False)


class TaskManager:
    """后台任务管理器"""

    def __init__(self):
        self._tasks: Dict[str, BackgroundTask] = {}

    def create_task(self, description: str) -> BackgroundTask:
        task_id = str(uuid.uuid4())[:8]
        task = BackgroundTask(id=task_id, description=description)
        self._tasks[task_id] = task
        return task

    async def start_task(self, task_id: str, coro: Coroutine) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False

        task.status = "running"

        async def _wrapper():
            try:
                result = await coro
                task.status = "completed"
                task.result = str(result) if result else None
            except asyncio.CancelledError:
                task.status = "cancelled"
            except Exception as e:
                task.status = "failed"
                task.error = str(e)
                logger.error(f"后台任务 {task_id} 失败: {e}")

        task._async_task = asyncio.create_task(_wrapper())
        return True

    def get_task(self, task_id: str) -> Optional[BackgroundTask]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list:
        return [
            {
                "id": t.id,
                "description": t.description,
                "status": t.status,
                "created_at": t.created_at,
                "error": t.error,
            }
            for t in self._tasks.values()
        ]

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task or not task._async_task:
            return False
        task._async_task.cancel()
        task.status = "cancelled"
        return True

    def cleanup_completed(self, max_keep: int = 100):
        completed = [
            tid for tid, t in self._tasks.items()
            if t.status in ("completed", "failed", "cancelled")
        ]
        for tid in completed[max_keep:]:
            del self._tasks[tid]
