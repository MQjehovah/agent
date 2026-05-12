import logging
from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from plugins import PluginManager

logger = logging.getLogger("agent.plugins")


class BasePlugin(ABC):
    name: str = ""
    description: str = ""
    version: str = "1.0.0"

    def __init__(self, config_path: str | None = None, config_dir: str | None = None):
        self.config_path = config_path
        self.config_dir = config_dir
        self.enabled = True
        self.plugin_manager: PluginManager | None = None
        self._agent = None
        self._load_config()

    @abstractmethod
    def _load_config(self):
        pass

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def stop(self):
        pass

    def set_plugin_manager(self, plugin_manager: "PluginManager"):
        self.plugin_manager = plugin_manager
        logger.debug(f"注册插件：{self.name}")

    def set_agent(self, agent):
        self._agent = agent

    def get_tool_defs(self) -> list[dict[str, Any]]:
        return []

    async def execute_tool(self, name: str, args: dict[str, Any]) -> str:
        return f"Tool {name} not implemented in plugin {self.name}"

    def get_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled
        }

    # ---- 生命周期钩子（可选覆盖） ----

    async def on_session_start(self, session_id: str, **kwargs):
        """新会话创建时触发"""
        pass

    async def on_session_end(self, session_id: str, **kwargs):
        """会话结束时触发"""
        pass

    async def on_pre_tool_call(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """工具调用前触发。返回非None则拦截执行，返回值作为工具结果。"""
        return None

    async def on_post_tool_call(self, tool_name: str, args: dict[str, Any], result: Any) -> Any:
        """工具调用后触发。可修改并返回结果。"""
        return result

    async def on_pre_llm_call(self, messages: list, tools: list) -> dict | None:
        """LLM 调用前触发。返回非None则替代LLM调用直接返回。"""
        return None

    async def on_post_llm_call(self, response: dict) -> dict:
        """LLM 调用后触发。可修改并返回响应。"""
        return response

    async def on_transform_tool_result(self, tool_name: str, result: str) -> str:
        """工具结果返回给LLM前的转换。"""
        return result
