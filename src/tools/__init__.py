import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional

logger = logging.getLogger("agent.tools")


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)

    def to_openai_format(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required
                }
            }
        }


class BuiltinTool(ABC):
    workspace: str = ""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        pass

    @property
    @abstractmethod
    def parameters(self) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        pass

    def resolve_path(self, path: str) -> str:
        """将路径解析为绝对路径。相对路径基于 workspace 解析。"""
        if not path:
            return self.workspace or os.getcwd()
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.workspace or os.getcwd(), path))

    def is_path_allowed(self, path: str) -> bool:
        """写操作路径边界检查。workspace 为空时允许所有路径。"""
        if not self.workspace:
            return True
        resolved = os.path.normpath(os.path.abspath(path))
        ws = os.path.normpath(os.path.abspath(self.workspace))
        return resolved == ws or resolved.startswith(ws + os.sep)

    def get_definition(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
            }
        }


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BuiltinTool] = {}
        self._workspace: str = ""

    @property
    def workspace(self) -> str:
        return self._workspace

    @workspace.setter
    def workspace(self, value: str):
        self._workspace = value
        for tool in self._tools.values():
            tool.workspace = value

    def register_tool(self, tool: BuiltinTool) -> bool:
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已存在，将被覆盖")
        tool.workspace = self._workspace
        self._tools[tool.name] = tool
        logger.debug(f"注册内置工具: {tool.name}")
        return True

    def unregister_tool(self, name: str) -> bool:
        if name not in self._tools:
            logger.warning(f"工具 '{name}' 不存在")
            return False
        del self._tools[name]
        logger.debug(f"注销内置工具: {name}")
        return True

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [tool.get_definition() for tool in self._tools.values()]

    async def execute(self, name: str, args: Dict[str, Any]) -> str:
        if name not in self._tools:
            logger.error(f"工具 '{name}' 未找到")
            return f"错误: 工具 '{name}' 未找到"

        tool = self._tools[name]
        try:
            result = await tool.execute(**args)
            return result
        except TypeError as e:
            logger.error(f"工具 '{name}' 参数错误: {e}")
            return f"错误: 工具 '{name}' 参数错误 - {e}"
        except Exception as e:
            logger.error(f"工具 '{name}' 执行失败: {e}")
            return f"错误: 工具 '{name}' 执行失败 - {e}"

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    def get_tool(self, name: str) -> Optional[BuiltinTool]:
        return self._tools.get(name)


# 核心工具
from .todo import TodoTool
from .file import FileTool
from .subagent import SubagentTool
from .memory import MemoryTool
from .shell import ShellTool

# 搜索与编辑工具
from .grep import GrepTool
from .glob import GlobTool
from .edit import EditTool
from .code_preview import CodePreviewTool

# Web 工具
from .web import WebSearchTool, WebFetchTool

# 后台任务工具
from .task import TaskCreateTool, TaskListTool, TaskGetTool, TaskCancelTool

# 用户交互工具
from .ask_user import AskUserTool

__all__ = [
    'ToolRegistry', 'BuiltinTool', 'ToolDefinition',
    # 核心
    'TodoTool', 'FileTool', 'SubagentTool', 'MemoryTool', 'ShellTool',
    # 搜索与编辑
    'GrepTool', 'GlobTool', 'EditTool', 'CodePreviewTool',
    # Web
    'WebSearchTool', 'WebFetchTool',
    # 后台任务
    'TaskCreateTool', 'TaskListTool', 'TaskGetTool', 'TaskCancelTool',
    # 用户交互
    'AskUserTool',
]
