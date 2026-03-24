import json
from typing import Dict, Any, List

from . import BuiltinTool


class SubagentTool(BuiltinTool):
    def __init__(self, subagent_manager):
        self.manager = subagent_manager

    @property
    def name(self) -> str:
        return "subagent"

    @property
    def description(self) -> str:
        return """子代理工具。用于创建和管理子代理(Subagent)来处理复杂或独立的任务。

使用场景:
- 需要将复杂任务分解为多个独立子任务并行处理
- 需要使用不同的系统提示词处理特定任务
- 需要隔离的任务执行环境

功能:
- create: 创建并执行子代理任务
- status: 查询子代理状态
- result: 获取子代理结果
- list: 列出所有子代理

示例:
{"action": "run", "task": "分析数据", "template": "data_analyst"}
{"action": "create", "task": "分析数据", "name": "data_analyst", "system_prompt": "你是一个数据分析师"}
{"action": "status", "subagent_id": "abc123"}
{"action": "list"}
{"action": "templates"}"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "status", "result", "list", "run", "templates"],
                    "description": "操作类型: create-创建并异步执行, run-创建并同步执行, status-查询状态, result-获取结果, list-列出子代理, templates-列出可用模板"
                },
                "task": {
                    "type": "string",
                    "description": "子代理要执行的任务内容(用于create/run操作)"
                },
                "template": {
                    "type": "string",
                    "description": "使用的模板名称，如 code_reviewer, data_analyst 等"
                },
                "name": {
                    "type": "string",
                    "description": "子代理名称，可选"
                },
                "system_prompt": {
                    "type": "string",
                    "description": "子代理的系统提示词，可选"
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "子代理可用的工具列表，为空则使用全部工具"
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "最大迭代次数，默认50"
                },
                "subagent_id": {
                    "type": "string",
                    "description": "子代理ID(用于status/result操作)"
                }
            },
            "required": ["action"]
        }

    async def execute(self, **kwargs) -> str:
        action = kwargs.get("action")
        
        if action == "create":
            return await self._create(kwargs)
        elif action == "run":
            return await self._run_sync(kwargs)
        elif action == "status":
            return self._status(kwargs.get("subagent_id"))
        elif action == "result":
            return self._result(kwargs.get("subagent_id"))
        elif action == "list":
            return self._list()
        elif action == "templates":
            return self._templates()
        else:
            return json.dumps({"success": False, "error": f"未知操作: {action}"}, ensure_ascii=False)

    async def _create(self, kwargs: Dict[str, Any]) -> str:
        task = kwargs.get("task")
        if not task:
            return json.dumps({"success": False, "error": "缺少task参数"}, ensure_ascii=False)
        
        import asyncio
        subagent_id = self.manager.create_subagent(
            task=task,
            name=kwargs.get("name", ""),
            system_prompt=kwargs.get("system_prompt", ""),
            tools=kwargs.get("tools"),
            max_iterations=kwargs.get("max_iterations", 50),
            template=kwargs.get("template", "")
        )
        
        asyncio.create_task(self.manager.run_subagent(subagent_id))
        
        return json.dumps({
            "success": True,
            "subagent_id": subagent_id,
            "message": f"子代理已创建并开始执行",
            "status_url": f"使用 action=status 查询状态"
        }, ensure_ascii=False)

    async def _run_sync(self, kwargs: Dict[str, Any]) -> str:
        task = kwargs.get("task")
        if not task:
            return json.dumps({"success": False, "error": "缺少task参数"}, ensure_ascii=False)
        
        result = await self.manager.create_and_run(
            task=task,
            name=kwargs.get("name", ""),
            system_prompt=kwargs.get("system_prompt", ""),
            tools=kwargs.get("tools"),
            max_iterations=kwargs.get("max_iterations", 50),
            template=kwargs.get("template", "")
        )
        
        return json.dumps({
            "success": result.status == "completed",
            "subagent_id": result.subagent_id,
            "status": result.status,
            "result": result.result,
            "iterations": result.iterations,
            "error": result.error
        }, ensure_ascii=False)

    def _status(self, subagent_id: str) -> str:
        if not subagent_id:
            return json.dumps({"success": False, "error": "缺少subagent_id参数"}, ensure_ascii=False)
        
        subagent = self.manager.subagents.get(subagent_id)
        if not subagent:
            return json.dumps({"success": False, "error": "子代理不存在"}, ensure_ascii=False)
        
        return json.dumps({
            "success": True,
            "subagent_id": subagent_id,
            "name": subagent.config.name,
            "status": subagent.status,
            "iterations": subagent.iterations,
            "task": subagent.config.task[:100]
        }, ensure_ascii=False)

    def _result(self, subagent_id: str) -> str:
        if not subagent_id:
            return json.dumps({"success": False, "error": "缺少subagent_id参数"}, ensure_ascii=False)
        
        result = self.manager.get_result(subagent_id)
        if not result:
            subagent = self.manager.subagents.get(subagent_id)
            if subagent:
                return json.dumps({
                    "success": False,
                    "status": subagent.status,
                    "message": "子代理尚未完成"
                }, ensure_ascii=False)
            return json.dumps({"success": False, "error": "子代理不存在"}, ensure_ascii=False)
        
        return json.dumps({
            "success": result.status == "completed",
            "subagent_id": result.subagent_id,
            "status": result.status,
            "result": result.result,
            "iterations": result.iterations,
            "error": result.error
        }, ensure_ascii=False)

    def _list(self) -> str:
        subagents = self.manager.list_subagents()
        return json.dumps({
            "success": True,
            "count": len(subagents),
            "subagents": subagents
        }, ensure_ascii=False)

    def _templates(self) -> str:
        templates = self.manager.list_templates()
        return json.dumps({
            "success": True,
            "count": len(templates),
            "templates": templates
        }, ensure_ascii=False)