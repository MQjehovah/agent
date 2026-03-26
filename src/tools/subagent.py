import json
from typing import Dict, Any, List

from . import BuiltinTool


class SubagentTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "subagent"

    @property
    def description(self) -> str:
        return """子代理工具。用于创建子代理(Subagent)来处理复杂或独立的任务。

使用场景:
- 需要将复杂任务分解为多个独立子任务并行处理
- 需要使用不同的系统提示词处理特定任务
- 需要隔离的任务执行环境
- 需要为子代理配置独立的MCP服务器

示例:
{"task": "分析数据", "template": "business_analyst"}
{"task": "分析数据", "name": "analyst", "system_prompt": "你是一个数据分析师"}
{"task": "查询库存", "template": "business_analyst", "mcp_servers": [{"name": "db", "command": "python", "args": ["mcp_server.py"]}]}

注意: 推荐在配置文件(config/agents/*.md)中预先定义subagent模板，包含mcp_servers配置"""

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "子代理要执行的任务内容"
                },
                "template": {
                    "type": "string",
                    "description": "使用的模板名称，如 code_reviewer, business_analyst 等"
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
                "mcp_servers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "MCP服务器名称"},
                            "command": {"type": "string", "description": "启动命令"},
                            "args": {"type": "array", "items": {"type": "string"}, "description": "命令参数"},
                            "env": {"type": "object", "description": "环境变量"}
                        }
                    },
                    "description": "子代理独立使用的MCP服务器配置列表"
                }
            },
            "required": ["task"]
        }

    async def execute(self, **kwargs) -> str:
        return json.dumps(kwargs, ensure_ascii=False)