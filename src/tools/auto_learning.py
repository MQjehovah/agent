import json
from typing import Dict, Any
from . import BuiltinTool


class AutoLearningTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "auto_learning"

    @property
    def description(self) -> str:
        return "自学习管理工具：查看学习建议、自动创建技能/子代理、优化提示词"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["suggestions", "create_skill", "create_subagent", "optimize_prompt", "patterns"],
                    "description": "操作类型：suggestions查看建议，create_skill自动创建技能，create_subagent自动创建子代理，optimize_prompt优化提示词，patterns查看任务模式统计"
                },
                "task_type": {
                    "type": "string",
                    "description": "任务类型（action=create_skill/create_subagent时使用）"
                }
            },
            "required": ["action"]
        }

    def __init__(self, agent=None):
        self._agent = agent

    def set_agent(self, agent):
        self._agent = agent

    async def execute(self, **kwargs) -> str:
        if not self._agent:
            return json.dumps({"success": False, "error": "Agent not initialized"}, ensure_ascii=False)

        action = kwargs.get("action")

        if action == "suggestions":
            suggestions = self._agent.get_auto_learning_suggestions()
            if not suggestions:
                return json.dumps({"success": True, "suggestions": [], "message": "当前没有自动创建建议，继续积累经验后会产生建议"}, ensure_ascii=False)
            return json.dumps({"success": True, "suggestions": suggestions}, ensure_ascii=False)

        elif action == "patterns":
            if not self._agent._pattern_tracker:
                return json.dumps({"success": False, "error": "模式追踪未启用"}, ensure_ascii=False)
            patterns = self._agent._pattern_tracker.get_hot_patterns(min_count=2)
            return json.dumps({"success": True, "patterns": patterns}, ensure_ascii=False)

        elif action in ("create_skill", "create_subagent", "optimize_prompt"):
            task_type = kwargs.get("task_type", "")
            if action in ("create_skill", "create_subagent") and not task_type:
                return json.dumps({"success": False, "error": "task_type is required"}, ensure_ascii=False)
            return await self._agent.apply_auto_learning(action, task_type)

        else:
            return json.dumps({"success": False, "error": f"Unknown action: {action}"}, ensure_ascii=False)
