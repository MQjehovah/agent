import json
import uuid
import os
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, List, Any, Optional

from . import BuiltinTool

logger = logging.getLogger("agent.tools")


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
        return "任务追踪工具。用于管理待办事项列表，支持添加、更新状态、设置优先级、按状态过滤等操作。"

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
                            "id": {
                                "type": "string",
                                "description": "已有任务的ID（更新时使用）"
                            },
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
                },
                "filter_status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "cancelled", "all"],
                    "description": "按状态过滤（返回结果中只包含该状态的任务）",
                    "default": "all"
                }
            },
            "required": ["todos"]
        }

    def __init__(self, persist_path: str = None):
        self._todos: Dict[str, TodoItem] = {}
        self._persist_path = persist_path
        if persist_path and os.path.exists(persist_path):
            self._load()

    def _load(self):
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                self._todos[item["id"]] = TodoItem(**item)
        except Exception as e:
            logger.warning(f"加载 todo 数据失败: {e}")

    def _save(self):
        if self._persist_path:
            try:
                with open(self._persist_path, "w", encoding="utf-8") as f:
                    json.dump(
                        [asdict(t) for t in self._todos.values()],
                        f, ensure_ascii=False, indent=2
                    )
            except Exception as e:
                logger.warning(f"保存 todo 数据失败: {e}")

    async def execute(self, todos: List[Dict], filter_status: str = "all") -> str:
        updated_count = 0
        added_count = 0

        for todo_data in todos:
            todo_id = todo_data.get("id")
            content = todo_data.get("content")

            if todo_id and todo_id in self._todos:
                # 更新已有任务
                todo = self._todos[todo_id]
                if content:
                    todo.content = content
                if "status" in todo_data:
                    todo.status = todo_data["status"]
                if "priority" in todo_data:
                    todo.priority = todo_data["priority"]
                updated_count += 1
            else:
                # 新增任务
                if not content:
                    continue
                new_id = str(uuid.uuid4())[:8]
                self._todos[new_id] = TodoItem(
                    id=new_id,
                    content=content,
                    status=todo_data.get("status", "pending"),
                    priority=todo_data.get("priority", "medium"),
                )
                added_count += 1

        self._save()

        # 过滤
        result_todos = self._get_filtered_todos(filter_status)

        result = {
            "success": True,
            "message": f"添加 {added_count} 个，更新 {updated_count} 个待办事项",
            "total_count": len(self._todos),
            "filtered_count": len(result_todos),
            "todos": [asdict(todo) for todo in result_todos]
        }

        return json.dumps(result, ensure_ascii=False)

    def _get_filtered_todos(self, filter_status: str) -> List[TodoItem]:
        if filter_status == "all":
            return list(self._todos.values())
        return [t for t in self._todos.values() if t.status == filter_status]

    def add_todo(self, content: str, priority: str = "medium") -> str:
        todo_id = str(uuid.uuid4())[:8]
        self._todos[todo_id] = TodoItem(
            id=todo_id,
            content=content,
            status="pending",
            priority=priority
        )
        self._save()
        return todo_id

    def update_status(self, todo_id: str, status: str) -> bool:
        if todo_id not in self._todos:
            return False
        valid_statuses = ["pending", "in_progress", "completed", "cancelled"]
        if status not in valid_statuses:
            return False
        self._todos[todo_id].status = status
        self._save()
        return True

    def get_todos(self, filter_status: str = "all") -> List[Dict]:
        return [asdict(todo) for todo in self._get_filtered_todos(filter_status)]

    def clear_completed(self) -> int:
        completed_ids = [
            todo_id for todo_id, todo in self._todos.items()
            if todo.status == "completed"
        ]
        for todo_id in completed_ids:
            del self._todos[todo_id]
        self._save()
        return len(completed_ids)

    def clear_all(self) -> int:
        count = len(self._todos)
        self._todos.clear()
        self._save()
        return count
