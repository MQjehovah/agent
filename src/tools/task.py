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


class TaskCreateTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_create"

    @property
    def description(self) -> str:
        return "创建一个后台任务。任务将在后台异步执行，不阻塞当前对话。"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "任务描述"
                }
            },
            "required": ["description"]
        }

    async def execute(self, **kwargs) -> str:
        description = kwargs.get("description", "")
        task = self.task_manager.create_task(description)
        return json.dumps({
            "success": True,
            "task_id": task.id,
            "status": task.status,
            "description": task.description
        }, ensure_ascii=False)


class TaskListTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_list"

    @property
    def description(self) -> str:
        return "列出所有后台任务及其状态"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        tasks = self.task_manager.list_tasks()
        return json.dumps({
            "success": True,
            "count": len(tasks),
            "tasks": tasks
        }, ensure_ascii=False)


class TaskGetTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_get"

    @property
    def description(self) -> str:
        return "获取指定后台任务的详细信息和结果"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "任务ID"}
            },
            "required": ["task_id"]
        }

    async def execute(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        task = self.task_manager.get_task(task_id)
        if not task:
            return json.dumps({"success": False, "error": f"任务不存在: {task_id}"}, ensure_ascii=False)
        return json.dumps({
            "success": True,
            "task": {
                "id": task.id,
                "description": task.description,
                "status": task.status,
                "created_at": task.created_at,
                "result": task.result,
                "error": task.error
            }
        }, ensure_ascii=False)


class TaskCancelTool(BuiltinTool):
    def __init__(self, task_manager: TaskManager):
        self.task_manager = task_manager

    @property
    def name(self) -> str:
        return "task_cancel"

    @property
    def description(self) -> str:
        return "取消一个正在运行的后台任务"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "要取消的任务ID"}
            },
            "required": ["task_id"]
        }

    async def execute(self, **kwargs) -> str:
        task_id = kwargs.get("task_id", "")
        success = await self.task_manager.cancel_task(task_id)
        return json.dumps({
            "success": success,
            "task_id": task_id
        }, ensure_ascii=False)
