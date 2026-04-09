import json
from typing import Dict, Any, Optional
from . import BuiltinTool


class SubagentPoolTool(BuiltinTool):
    def __init__(self, subagent_manager=None):
        self.subagent_manager = subagent_manager

    def set_subagent_manager(self, manager):
        self.subagent_manager = manager

    @property
    def name(self) -> str:
        return "subagent_pool"

    @property
    def description(self) -> str:
        return """子代理并发池管理工具。用于查询并发任务状态、池统计信息。

使用场景:
- 查询并发任务的执行状态
- 查看并发池的负载情况
- 管理任务队列

示例:
{"action": "status", "task_id": "task_1_abc123"}
{"action": "list", "status_filter": "running"}
{"action": "stats"}
"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "list", "stats"],
                    "description": "操作类型: status(查询任务), list(列出任务), stats(池统计)"
                },
                "task_id": {
                    "type": "string",
                    "description": "任务ID（action=status时必需）"
                },
                "status_filter": {
                    "type": "string",
                    "enum": ["pending", "running", "completed", "failed"],
                    "description": "状态过滤（action=list时使用）"
                }
            },
            "required": ["action"]
        }

    async def execute(self, **kwargs) -> str:
        action = kwargs.get("action")

        if not self.subagent_manager:
            return json.dumps({"error": "子代理管理器未初始化"}, ensure_ascii=False)

        if action == "stats":
            stats = self.subagent_manager.get_pool_stats()
            return json.dumps({"success": True, "stats": stats}, ensure_ascii=False)

        elif action == "list":
            status_filter = kwargs.get("status_filter")
            tasks = self.subagent_manager.get_all_tasks(status_filter)
            return json.dumps({"success": True, "tasks": tasks}, ensure_ascii=False)

        elif action == "status":
            task_id = kwargs.get("task_id")
            if not task_id:
                return json.dumps({"error": "缺少task_id参数"}, ensure_ascii=False)
            status = self.subagent_manager.get_task_status(task_id)
            if status:
                return json.dumps({"success": True, "task": status}, ensure_ascii=False)
            return json.dumps({"error": f"任务 {task_id} 不存在"}, ensure_ascii=False)

        return json.dumps({"error": f"未知操作: {action}"}, ensure_ascii=False)
