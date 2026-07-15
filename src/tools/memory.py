import json
from typing import Any

from . import BuiltinTool


class MemoryTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return "记忆管理工具，用于保存、搜索和列出记忆"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["save", "search", "list"],
                    "description": "操作类型：save保存记忆，search搜索记忆，list列出记忆"
                },
                "content": {
                    "type": "string",
                    "description": "要保存的记忆内容（action=save时使用）"
                },
                "category": {
                    "type": "string",
                    "enum": ["preference", "key_info", "todo", "knowledge", "failure_lesson", "correction", "reflection"],
                    "description": "记忆分类（action=save时使用）"
                },
                "query": {
                    "type": "string",
                    "description": "搜索关键词（action=search时使用）"
                },
                "memory_type": {
                    "type": "string",
                    "enum": ["daily", "long_term"],
                    "description": "记忆类型（action=list时使用）"
                }
            },
            "required": ["action"]
        }

    def __init__(self, memory_manager=None):
        self.memory_manager = memory_manager

    def set_memory_manager(self, manager):
        self.memory_manager = manager

    async def execute(self, **kwargs) -> str:
        action = kwargs.get("action")

        if not self.memory_manager:
            return json.dumps({"success": False, "error": "Memory manager not initialized"}, ensure_ascii=False)

        if action == "save":
            return self._save(kwargs)
        elif action == "search":
            return self._search(kwargs)
        elif action == "list":
            return self._list(kwargs)
        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"}, ensure_ascii=False)

    def _save(self, args: dict[str, Any]) -> str:
        category = args.get("category", "key_info")
        content = args.get("content", "")
        user_id = args.get("_local_user_id", "")

        if not content:
            return json.dumps({"success": False, "error": "Content is required"}, ensure_ascii=False)

        if category == "preference":
            self.memory_manager.add_preference(user_id, content)
        elif category == "key_info":
            self.memory_manager.add_key_info(user_id, content)
        elif category == "todo":
            self.memory_manager.add_todo(user_id, content)
        elif category == "knowledge":
            self.memory_manager.add_key_info(user_id, f"[知识] {content}")
        elif category == "failure_lesson":
            self.memory_manager.add_failure_lesson(user_id, "manual", content, "")
        elif category == "correction":
            self.memory_manager.add_correction(user_id, content, "")
        elif category == "reflection":
            self.memory_manager.add_reflection(user_id, content)

        return json.dumps({"success": True, "message": f"Memory saved to {category}"}, ensure_ascii=False)

    def _search(self, args: dict[str, Any]) -> str:
        user_id = args.get("_local_user_id", "")
        results = self.memory_manager.load_memory(user_id, task=args.get("query", ""))

        if results:
            return json.dumps({"success": True, "results": results}, ensure_ascii=False)
        return json.dumps({"success": True, "results": "No matching memories found"}, ensure_ascii=False)

    def _list(self, args: dict[str, Any]) -> str:
        return json.dumps({"success": True, "files": [], "note": "记忆已迁移至数据库，按用户隔离"}, ensure_ascii=False)
