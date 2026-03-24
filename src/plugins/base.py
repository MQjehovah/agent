import logging
from abc import ABC, abstractmethod
from typing import Optional, Callable, Dict, Any

logger = logging.getLogger("agent.plugins")


class BasePlugin(ABC):
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.enabled = True
        self.agent_executor: Optional[Callable] = None
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
    
    def register_agent(self, executor: Callable):
        self.agent_executor = executor
        logger.info(f"Plugin [{self.name}] agent registered")
    
    def get_info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": self.enabled
        }