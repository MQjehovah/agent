import json
from typing import Dict, Any, List

from . import BuiltinTool


class SubagentTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "subagent"

    @property
    def description(self) -> str:
        return """子代理工具。用于创建或复用子代理(Subagent)来处理复杂或独立的任务。

使用场景:
- 需要将复杂任务分解为多个独立子任务并行处理
- 需要使用不同的系统提示词处理特定任务
- 需要隔离的任务执行环境
- 需要为子代理配置独立的MCP服务器
- 需要保持子代理上下文连续性（通过session_id复用）

重要特性:
- 子代理默认保持存活，不会被释放
- 通过session_id可以复用已有的子代理，保持上下文连续性
- 通过template/name可以自动找到并复用已创建的子代理
- 程序退出时才会释放所有子代理

示例:
{"task": "分析数据", "template": "business_analyst"}
{"task": "继续分析", "session_id": "abc123"}
{"task": "查询库存", "template": "business_analyst", "mcp_servers": [{"name": "db", "command": "python", "args": ["mcp_server.py"]}]}
{"task": "一次性任务", "template": "analyst", "keep_alive": false}

注意: 推荐在配置文件(workspace/agents/*.md)中预先定义subagent模板，包含mcp_servers配置"""

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
                    "description": "使用的模板名称，如 设备运维, 数字中台, IT运维 等。如果该模板的子代理已存在，会自动复用"
                },
                "name": {
                    "type": "string",
                    "description": "子代理名称，可选。如果该名称的子代理已存在，会自动复用"
                },
                "session_id": {
                    "type": "string",
                    "description": "会话ID，用于复用已有的子代理实例。如果不提供，会自动生成或根据template/name查找已有实例"
                },
                "system_prompt": {
                    "type": "string",
                    "description": "子代理的系统提示词，可选。仅创建新子代理时使用"
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
                },
                "keep_alive": {
                    "type": "boolean",
                    "description": "是否保持子代理存活（默认true）。设为false则在任务完成后释放子代理",
                    "default": True
                }
            },
            "required": ["task"]
        }

    async def execute(self, **kwargs) -> str:
        return json.dumps(kwargs, ensure_ascii=False)