import json
import logging
import os
import time
import uuid
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger("agent.autonomous.panel")


@dataclass
class PanelTask:
    id: str
    title: str
    description: str = ""
    priority: int = 3  # 1=高 2=中 3=低
    status: str = "pending"  # pending/active/completed
    source: str = "llm"  # user/llm/event
    interval: int | None = None  # 秒，null=一次性
    last_run: float | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "status": self.status,
            "source": self.source,
            "interval": self.interval,
            "last_run": self.last_run,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PanelTask":
        return cls(
            id=data.get("id", uuid.uuid4().hex[:12]),
            title=data["title"],
            description=data.get("description", ""),
            priority=data.get("priority", 3),
            status=data.get("status", "pending"),
            source=data.get("source", "llm"),
            interval=data.get("interval"),
            last_run=data.get("last_run"),
            created_at=data.get("created_at", datetime.now().isoformat()),
        )

    @property
    def is_due(self) -> bool:
        if self.status != "pending":
            return False
        if self.interval is None:
            return True
        if self.last_run is None:
            return True
        return (time.time() - self.last_run) >= self.interval


class TaskPanel:
    def __init__(self, panel_path: str):
        self.panel_path = panel_path
        self._tasks: dict[str, PanelTask] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.panel_path):
            return
        try:
            with open(self.panel_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("tasks", []):
                task = PanelTask.from_dict(item)
                self._tasks[task.id] = task
            logger.info("任务面板已加载: %d 个任务", len(self._tasks))
        except Exception:
            logger.exception("加载任务面板失败")

    def _save(self):
        os.makedirs(os.path.dirname(self.panel_path) or ".", exist_ok=True)
        data = {"tasks": [t.to_dict() for t in self._tasks.values()]}
        with open(self.panel_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def add_task(
        self,
        title: str,
        description: str = "",
        priority: int = 3,
        interval: int | None = None,
        source: str = "user",
    ) -> PanelTask:
        task = PanelTask(
            id=uuid.uuid4().hex[:12],
            title=title,
            description=description,
            priority=priority,
            source=source,
            interval=interval,
        )
        self._tasks[task.id] = task
        self._save()
        logger.info("任务已添加到面板: [%s] %s", source, title)
        return task

    def remove_task(self, task_id: str) -> bool:
        if task_id in self._tasks:
            del self._tasks[task_id]
            self._save()
            return True
        return False

    def mark_active(self, task_id: str):
        if task_id in self._tasks:
            self._tasks[task_id].status = "active"
            self._save()

    def mark_completed(self, task_id: str):
        if task_id in self._tasks:
            self._tasks[task_id].status = "completed"
            self._tasks[task_id].last_run = time.time()
            self._save()

    def mark_pending(self, task_id: str):
        """标记为 pending（重复任务执行完重置状态）"""
        if task_id in self._tasks:
            t = self._tasks[task_id]
            t.status = "pending"
            t.last_run = time.time()
            self._save()

    def get_pending(self) -> list[PanelTask]:
        """获取到期的 pending 任务，按优先级排序"""
        due = [t for t in self._tasks.values() if t.is_due]
        due.sort(key=lambda t: (t.priority, t.created_at))
        return due

    def list_all(self, source: str | None = None) -> list[PanelTask]:
        tasks = list(self._tasks.values())
        if source:
            tasks = [t for t in tasks if t.source == source]
        tasks.sort(key=lambda t: (t.status, t.priority, t.created_at))
        return tasks

    def get_stats(self) -> dict:
        all_tasks = list(self._tasks.values())
        return {
            "total": len(all_tasks),
            "pending": sum(1 for t in all_tasks if t.status == "pending"),
            "active": sum(1 for t in all_tasks if t.status == "active"),
            "completed": sum(1 for t in all_tasks if t.status == "completed"),
            "by_source": {
                "user": sum(1 for t in all_tasks if t.source == "user"),
                "llm": sum(1 for t in all_tasks if t.source == "llm"),
                "event": sum(1 for t in all_tasks if t.source == "event"),
            },
        }

    def is_empty(self) -> bool:
        return len(self._tasks) == 0
