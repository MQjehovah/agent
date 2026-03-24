import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Any

from . import BuiltinTool


@dataclass
class TodoItem:
    id: str
    content: str
    status: str = "pending"
    priority: str = "medium"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class TodoTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "todowrite"

    @property
    def description(self) -> str:
        return "任务追踪工具。用于管理待办事项列表，支持添加、更新状态、设置优先级等操作。可以帮助追踪任务进度和管理工作流程。"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "待办事项列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "任务内容"
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "任务状态"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "任务优先级"
                            }
                        },
                        "required": ["content"]
                    }
                }
            },
            "required": ["todos"]
        }

    def __init__(self):
        self._todos: Dict[str, TodoItem] = {}

    async def execute(self, todos: List[Dict]) -> str:
        updated_count = 0
        added_count = 0

        for todo_data in todos:
            content = todo_data.get("content")
            if not content:
                continue

            todo_id = str(uuid.uuid4())[:8]
            status = todo_data.get("status", "pending")
            priority = todo_data.get("priority", "medium")

            self._todos[todo_id] = TodoItem(
                id=todo_id,
                content=content,
                status=status,
                priority=priority
            )
            added_count += 1

        result = {
            "success": True,
            "message": f"成功添加 {added_count} 个待办事项",
            "total_count": len(self._todos),
            "todos": [asdict(todo) for todo in self._todos.values()]
        }

        return json.dumps(result, ensure_ascii=False)

    def add_todo(self, content: str, priority: str = "medium") -> str:
        todo_id = str(uuid.uuid4())[:8]
        self._todos[todo_id] = TodoItem(
            id=todo_id,
            content=content,
            status="pending",
            priority=priority
        )
        return todo_id

    def update_status(self, todo_id: str, status: str) -> bool:
        if todo_id not in self._todos:
            return False

        valid_statuses = ["pending", "in_progress", "completed", "cancelled"]
        if status not in valid_statuses:
            return False

        self._todos[todo_id].status = status
        return True

    def get_todos(self) -> List[Dict]:
        return [asdict(todo) for todo in self._todos.values()]

    def clear_completed(self) -> int:
        completed_ids = [
            todo_id for todo_id, todo in self._todos.items()
            if todo.status == "completed"
        ]
        for todo_id in completed_ids:
            del self._todos[todo_id]
        return len(completed_ids)

    def clear_all(self) -> int:
        count = len(self._todos)
        self._todos.clear()
        return count