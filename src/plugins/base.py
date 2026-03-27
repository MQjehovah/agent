import logging
from abc import ABC, abstractmethod
from typing import Optional, Callable, Dict, Any, List, TYPE_CHECKING

if TYPE_CHECKING:
    from plugins import PluginManager

logger = logging.getLogger("agent.plugins")


class BasePlugin(ABC):
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.enabled = True
        self.plugin_manager: Optional["PluginManager"] = None
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
    
    def get_tool_defs(self) -> List[Dict[str, Any]]:
        return []
    
    async def execute_tool(self, name: str, args: Dict[str, Any]) -> str:
        return f"Tool {name} not implemented in plugin {self.name}"
    
    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled
        }