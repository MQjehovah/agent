import json
from typing import Dict, Any
from . import BuiltinTool
from learning.categories import MemoryCategory


class MemoryTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "memory"

    @property
    def description(self) -> str:
        return "记忆管理工具，用于保存、搜索和列出记忆"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["save", "search", "list", "share"],
                    "description": "操作类型：save保存记忆，search搜索记忆，list列出记忆文件，share共享知识"
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

    def __init__(self, memory_manager=None, memory_writer=None):
        self.memory_manager = memory_manager
        self.memory_writer = memory_writer

    def set_memory_manager(self, manager):
        self.memory_manager = manager

    def set_memory_writer(self, writer):
        self.memory_writer = writer

    async def execute(self, **kwargs) -> str:
        action = kwargs.get("action")

        if action == "save":
            return self._save(kwargs)
        elif action == "search":
            return self._search(kwargs)
        elif action == "list":
            return self._list(kwargs)
        elif action == "share":
            return self._share(kwargs)
        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"}, ensure_ascii=False)

    def _save(self, args: Dict[str, Any]) -> str:
        if not self.memory_writer:
            return json.dumps({"success": False, "error": "Memory writer not initialized"}, ensure_ascii=False)

        category = args.get("category", "key_info")
        content = args.get("content", "")

        if not content:
            return json.dumps({"success": False, "error": "Content is required"}, ensure_ascii=False)

        category_map = {
            "preference": MemoryCategory.PREFERENCE,
            "key_info": MemoryCategory.KEY_INFO,
            "todo": MemoryCategory.TODO,
            "knowledge": MemoryCategory.KEY_INFO,
            "failure_lesson": MemoryCategory.FAILURE_LESSON,
            "correction": MemoryCategory.CORRECTION,
            "reflection": MemoryCategory.REFLECTION,
        }

        mem_category = category_map.get(category, MemoryCategory.KEY_INFO)
        if category == "knowledge":
            content = f"[知识] {content}"

        ok = self.memory_writer.write(mem_category, content)
        if ok:
            return json.dumps({"success": True, "message": f"Memory saved to {mem_category.value}"}, ensure_ascii=False)
        return json.dumps({"success": False, "error": "Failed to write memory"}, ensure_ascii=False)

    def _search(self, args: Dict[str, Any]) -> str:
        if not self.memory_manager:
            return json.dumps({"success": False, "error": "Memory manager not initialized"}, ensure_ascii=False)

        query = args.get("query", "")
        results = self.memory_manager.load_memory(query)

        if results:
            return json.dumps({"success": True, "results": results}, ensure_ascii=False)
        return json.dumps({"success": True, "results": "No matching memories found"}, ensure_ascii=False)

    def _list(self, args: Dict[str, Any]) -> str:
        if not self.memory_manager:
            return json.dumps({"success": False, "error": "Memory manager not initialized"}, ensure_ascii=False)

        memory_type = args.get("memory_type", "daily")

        if memory_type == "daily":
            files = self.memory_manager.list_daily_files()
            return json.dumps({"success": True, "files": sorted(files, reverse=True)}, ensure_ascii=False)
        elif memory_type == "long_term":
            return json.dumps({"success": True, "files": [self.memory_manager.long_term_file]}, ensure_ascii=False)
        return json.dumps({"success": True, "files": []}, ensure_ascii=False)

    def _share(self, args: Dict[str, Any]) -> str:
        if not self.memory_writer:
            return json.dumps({"success": False, "error": "Memory writer not initialized"}, ensure_ascii=False)

        content = args.get("content", "")
        if not content:
            return json.dumps({"success": False, "error": "Content is required for sharing"}, ensure_ascii=False)

        agent_id = self.memory_manager.agent_id if self.memory_manager else "unknown"
        ok = self.memory_writer.share_knowledge(agent_id, content)
        if ok:
            return json.dumps({"success": True, "message": "Knowledge shared"}, ensure_ascii=False)
        return json.dumps({"success": False, "error": "Failed to share knowledge"}, ensure_ascii=False)