import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")
VALID_PRIORITIES = ("high", "medium", "low")


@dataclass
class TodoItem:
    id: str
    content: str
    status: str = "pending"
    priority: str = "medium"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class TodoTool(BuiltinTool):
    """待办列表工具（整表替换语义，对齐 Claude Code TodoWrite）。"""

    @property
    def name(self) -> str:
        return "todowrite"

    @property
    def description(self) -> str:
        return (
            "待办列表工具。每次调用传入**当前所有**待办事项的完整列表，替换整个列表"
            "（不是增量更新：每次都要把已有项连同新状态一起重发）。\n"
            "用途：多步骤任务的进度展示与自我跟踪；简单/单步任务不必使用。\n"
            "约定：同一时刻至多一个 `in_progress`；开始一项就置为 in_progress，完成立即置 completed，"
            "不在计划中但已放弃的置 cancelled。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "待办事项完整列表（每次传入当前所有任务，会替换整个列表）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "已有任务的 ID（可选；缺省自动生成）"},
                            "content": {"type": "string", "description": "任务内容（必需）"},
                            "status": {
                                "type": "string",
                                "enum": list(VALID_STATUSES),
                                "description": "任务状态（默认 pending）",
                            },
                            "priority": {
                                "type": "string",
                                "enum": list(VALID_PRIORITIES),
                                "description": "优先级（默认 medium）",
                            },
                        },
                        "required": ["content"],
                    },
                }
            },
            "required": ["todos"],
        }

    def __init__(self, persist_path: str = None):
        self._todos: dict[str, TodoItem] = {}
        self._persist_path = persist_path
        if persist_path and os.path.exists(persist_path):
            self._load()

    # ── 持久化 ───────────────────────────────────────

    def _load(self):
        try:
            with open(self._persist_path, encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                self._todos[item["id"]] = TodoItem(**item)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"加载 todo 数据失败: {e}")

    def _save(self):
        if not self._persist_path:
            return
        try:
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump([asdict(t) for t in self._todos.values()], f,
                          ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"保存 todo 数据失败: {e}")

    # ── 工具入口：整表替换 ───────────────────────────

    async def execute(self, todos: list[dict] = None, **kwargs) -> str:
        if not isinstance(todos, list):
            return self._error("todos 必须是数组")

        new_todos: dict[str, TodoItem] = {}
        for i, item in enumerate(todos):
            if not isinstance(item, dict):
                return self._error(f"第 {i + 1} 项必须是对象")
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                return self._error(f"第 {i + 1} 项缺少非空 content")
            status = item.get("status", "pending")
            if status not in VALID_STATUSES:
                return self._error(
                    f"第 {i + 1} 项 status 非法: {status!r}（可选 {', '.join(VALID_STATUSES)}）")
            priority = item.get("priority", "medium")
            if priority not in VALID_PRIORITIES:
                return self._error(
                    f"第 {i + 1} 项 priority 非法: {priority!r}（可选 {', '.join(VALID_PRIORITIES)}）")
            todo_id = str(item.get("id") or uuid.uuid4().hex[:8])
            if todo_id in new_todos:
                return self._error(f"重复的 id: {todo_id}")
            new_todos[todo_id] = TodoItem(
                id=todo_id, content=content, status=status, priority=priority)

        self._todos = new_todos
        self._save()

        return json.dumps({
            "success": True,
            "message": f"已更新待办列表，共 {len(self._todos)} 项",
            "total_count": len(self._todos),
            "todos": [asdict(t) for t in self._todos.values()],
        }, ensure_ascii=False)

    # ── 读取/维护（供 Web/任务面板与测试使用，非 LLM 工具） ──

    def _get_filtered_todos(self, filter_status: str = "all") -> list[TodoItem]:
        if filter_status == "all":
            return list(self._todos.values())
        return [t for t in self._todos.values() if t.status == filter_status]

    def get_todos(self, filter_status: str = "all") -> list[dict]:
        return [asdict(todo) for todo in self._get_filtered_todos(filter_status)]

    def add_todo(self, content: str, priority: str = "medium") -> str:
        todo_id = uuid.uuid4().hex[:8]
        self._todos[todo_id] = TodoItem(
            id=todo_id, content=content, status="pending",
            priority=priority if priority in VALID_PRIORITIES else "medium")
        self._save()
        return todo_id

    def update_status(self, todo_id: str, status: str) -> bool:
        if todo_id not in self._todos or status not in VALID_STATUSES:
            return False
        self._todos[todo_id].status = status
        self._save()
        return True

    def clear_completed(self) -> int:
        completed = [tid for tid, t in self._todos.items() if t.status == "completed"]
        for tid in completed:
            del self._todos[tid]
        self._save()
        return len(completed)

    def clear_all(self) -> int:
        count = len(self._todos)
        self._todos.clear()
        self._save()
        return count

    @staticmethod
    def _error(msg: str) -> str:
        return json.dumps({"success": False, "error": msg}, ensure_ascii=False)
