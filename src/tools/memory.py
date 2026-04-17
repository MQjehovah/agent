import json
import os
from typing import Dict, Any
from . import BuiltinTool


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
                    "enum": ["save", "search", "list", "delete", "share"],
                    "description": "操作类型：save保存记忆，search搜索记忆，list列出记忆文件，delete删除记忆，share共享知识到跨代理知识库"
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
            return await self._save(kwargs)
        elif action == "search":
            return await self._search(kwargs)
        elif action == "list":
            return await self._list(kwargs)
        elif action == "delete":
            return await self._delete(kwargs)
        elif action == "share":
            return await self._share(kwargs)
        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"}, ensure_ascii=False)
    
    async def _save(self, args: Dict[str, Any]) -> str:
        category = args.get("category", "key_info")
        content = args.get("content", "")
        
        if not content:
            return json.dumps({"success": False, "error": "Content is required"}, ensure_ascii=False)
        
        if category == "preference":
            self.memory_manager.add_preference(content)
        elif category == "key_info":
            self.memory_manager.add_key_info(content)
        elif category == "todo":
            self.memory_manager.add_todo(content)
        elif category == "knowledge":
            self.memory_manager.add_key_info(f"[知识] {content}")
        elif category == "failure_lesson":
            self.memory_manager.add_failure_lesson("manual", content, "")
        elif category == "correction":
            self.memory_manager.add_correction(content, "")
        elif category == "reflection":
            self.memory_manager.add_reflection(content)
        
        return json.dumps({"success": True, "message": f"Memory saved to {category}"}, ensure_ascii=False)
    
    async def _search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "")
        results = self.memory_manager.load_memory(query)
        
        if results:
            return json.dumps({"success": True, "results": results}, ensure_ascii=False)
        return json.dumps({"success": True, "results": "No matching memories found"}, ensure_ascii=False)
    
    async def _list(self, args: Dict[str, Any]) -> str:
        memory_type = args.get("memory_type", "daily")

        if memory_type == "daily":
            files = self.memory_manager.list_daily_files()
            return json.dumps({"success": True, "files": sorted(files, reverse=True)}, ensure_ascii=False)
        elif memory_type == "long_term":
            return json.dumps({"success": True, "files": [self.memory_manager.long_term_file]}, ensure_ascii=False)
        else:
            return json.dumps({"success": True, "files": []}, ensure_ascii=False)

    async def _delete(self, args: Dict[str, Any]) -> str:
        content = args.get("content", "")
        if not content:
            return json.dumps({"success": False, "error": "Content is required for deletion"}, ensure_ascii=False)
        try:
            self.memory_manager.remove_memory(content)
            return json.dumps({"success": True, "message": "Memory deleted"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    async def _share(self, args: Dict[str, Any]) -> str:
        content = args.get("content", "")
        if not content:
            return json.dumps({"success": False, "error": "Content is required for sharing"}, ensure_ascii=False)
        try:
            agent_id = self.memory_manager.agent_id or "unknown"
            self.memory_manager.share_knowledge(agent_id, content)
            return json.dumps({"success": True, "message": "Knowledge shared to cross-agent knowledge base"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)