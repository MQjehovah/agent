import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("agent.tools")


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)

    def to_openai_format(self) -> dict[str, Any]:
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
    temp_dir: str = ""

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
    def parameters(self) -> dict[str, Any]:
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        pass

    def resolve_path(self, path: str) -> str:
        """将路径解析为绝对路径。相对路径基于 workspace（单一工作目录，读写一致）。

        参照 Claude Code / opencode：所有文件操作默认工作目录，不区分中间产物与
        交付物；需要整洁时由 agent 提示写入子目录（如 reports/）。temp_dir 仅用于
        agent 内部真正的临时文件，不作为 LLM 的默认写入区。
        """
        if not path:
            return self.workspace or os.getcwd()
        if os.path.isabs(path):
            return os.path.normpath(path)
        return os.path.normpath(os.path.join(self.workspace or os.getcwd(), path))

    def is_path_allowed(self, path: str) -> bool:
        """写操作路径边界检查。允许 workspace 和 temp_dir 内的路径。"""
        allowed = []
        if self.workspace:
            allowed.append(os.path.normpath(os.path.abspath(self.workspace)))
        if self.temp_dir:
            allowed.append(os.path.normpath(os.path.abspath(self.temp_dir)))
        if not allowed:
            return True
        resolved = os.path.normpath(os.path.abspath(path))
        return any(resolved == a or resolved.startswith(a + os.sep) for a in allowed)

    def get_definition(self) -> dict[str, Any]:
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
        self._tools: dict[str, BuiltinTool] = {}
        self._workspace: str = ""
        self._temp_dir: str = ""

    @property
    def workspace(self) -> str:
        return self._workspace

    @workspace.setter
    def workspace(self, value: str):
        self._workspace = value
        for tool in self._tools.values():
            tool.workspace = value

    @property
    def temp_dir(self) -> str:
        return self._temp_dir

    @temp_dir.setter
    def temp_dir(self, value: str):
        self._temp_dir = value
        for tool in self._tools.values():
            tool.temp_dir = value

    def register_tool(self, tool: BuiltinTool) -> bool:
        if tool.name in self._tools:
            logger.warning(f"工具 '{tool.name}' 已存在，将被覆盖")
        tool.workspace = self._workspace
        tool.temp_dir = self._temp_dir
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

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [tool.get_definition() for tool in self._tools.values()]

    async def execute(self, name: str, args: dict[str, Any]) -> str:
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

    def list_tools(self) -> list[str]:
        return list(self._tools.keys())

    def get_tool(self, name: str) -> BuiltinTool | None:
        return self._tools.get(name)

    def auto_discover(self, tools_dir: str = None, **extra_instances):
        """自动扫描 tools 目录，实例化所有 BuiltinTool 子类并注册。

        extra_instances: 部分工具需要外部依赖（如 TaskCreateTool 需要 TaskManager），
                         通过关键字参数传入已实例化的工具对象。
        """
        import importlib
        from pathlib import Path

        if tools_dir is None:
            tools_dir = os.path.dirname(os.path.abspath(__file__))

        tools_path = Path(tools_dir)
        skip = {"__init__.py", "__pycache__"}
        registered_names: dict[str, str] = {}

        for py_file in sorted(tools_path.glob("*.py")):
            if py_file.name in skip:
                continue

            module_name = py_file.stem

            try:
                source = py_file.read_text(encoding="utf-8")
            except Exception as e:
                logger.debug(f"读取工具源码失败 {module_name}: {e}")
                continue

            tool_classes = _find_tool_classes(source)
            if not tool_classes:
                continue

            try:
                mod = importlib.import_module(f".{module_name}", package="tools")
            except Exception as e:
                logger.warning(f"跳过工具模块 {module_name}: {e}")
                continue

            for cls_name in tool_classes:
                cls = getattr(mod, cls_name, None)
                if cls is None:
                    continue
                if not (isinstance(cls, type) and issubclass(cls, BuiltinTool) and cls is not BuiltinTool):
                    continue

                if cls_name in extra_instances:
                    instance = extra_instances[cls_name]
                else:
                    try:
                        instance = cls()
                    except TypeError:
                        logger.debug(f"跳过需要构造参数的工具: {cls_name}")
                        continue

                self.register_tool(instance)
                registered_names[instance.name] = cls_name

        if registered_names:
            logger.debug(f"自动发现并注册 {len(registered_names)} 个工具: {list(registered_names.keys())}")


def _find_tool_classes(source: str) -> list[str]:
    """AST 扫描源码，找到所有继承 BuiltinTool 的类名"""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "BuiltinTool":
                result.append(node.name)
                break
            if isinstance(base, ast.Attribute) and base.attr == "BuiltinTool":
                result.append(node.name)
                break
    return result


# 核心工具
# 用户交互工具
from .ask_user import AskUserTool
from .edit import EditTool
from .file import FileTool
from .glob import GlobTool

# 搜索与编辑工具
from .batch_edit import BatchEditTool
from .code_search import CodeSearchTool
from .grep import GrepTool
from .memory import MemoryTool
from .shell import ShellTool
from .subagent import SubagentTool

from .todo import TodoTool

# Web 工具
from .web import WebFetchTool, WebSearchTool

__all__ = [
    'ToolRegistry', 'BuiltinTool', 'ToolDefinition',
    'AskUserTool',
    'TodoTool', 'FileTool', 'SubagentTool', 'MemoryTool', 'ShellTool',
    'GrepTool', 'GlobTool', 'EditTool',
    'CodeSearchTool', 'BatchEditTool',
    'WebSearchTool', 'WebFetchTool',
]
