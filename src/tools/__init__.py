import logging
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

    def register_tool(self, tool: BuiltinTool) -> bool:
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已存在，将被覆盖")
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


from .todo import TodoTool

__all__ = ['ToolRegistry', 'BuiltinTool', 'ToolDefinition', 'TodoTool']