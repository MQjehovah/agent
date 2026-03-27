import os
import importlib
import logging
from typing import Dict, List, Type, Callable, Optional, Any

from .base import BasePlugin

logger = logging.getLogger("agent.plugins")


class PluginManager:
    PLUGIN_ENTRY = "plugin"
    
    def __init__(self, plugins_dir: Optional[str] = None):
        self.plugins_dir = plugins_dir
        self.plugins: Dict[str, BasePlugin] = {}
        self._plugin_classes: Dict[str, Type[BasePlugin]] = {}
        self._executor: Optional[Callable] = None
    
    def discover(self) -> List[str]:
        if not self.plugins_dir or not os.path.exists(self.plugins_dir):
            logger.warning(f"Plugins directory not found: {self.plugins_dir}")
            return []
        
        discovered = []
        for name in os.listdir(self.plugins_dir):
            plugin_path = os.path.join(self.plugins_dir, name)
            if os.path.isdir(plugin_path):
                init_file = os.path.join(plugin_path, "__init__.py")
                if os.path.exists(init_file):
                    discovered.append(name)
        
        return discovered
    
    def load_plugin(self, name: str, config_path: Optional[str] = None) -> Optional[BasePlugin]:
        if name in self.plugins:
            return self.plugins[name]
        
        try:
            module = importlib.import_module(f"plugins.{name}")
            
            plugin_class = getattr(module, self.PLUGIN_ENTRY, None)
            if not plugin_class:
                plugin_class = getattr(module, f"{name.capitalize()}Plugin", None)
            
            if not plugin_class:
                logger.error(f"Plugin class not found in: {name}")
                return None
            
            plugin = plugin_class(config_path=config_path)
            self.plugins[name] = plugin
            logger.debug(f"加载插件: {name}")
            return plugin
            
        except Exception as e:
            logger.error(f"Failed to load plugin {name}: {e}")
            return None
    
    def load_all(self) -> int:
        discovered = self.discover()
        loaded = 0
        
        for name in discovered:
            if self.load_plugin(name):
                loaded += 1
        
        logger.info(f"Loaded {loaded}/{len(discovered)} plugins")
        return loaded
    
    def register_executor(self, executor: Callable):
        self._executor = executor
        for plugin in self.plugins.values():
            if plugin.enabled:
                plugin.set_plugin_manager(self)
    
    async def execute(self, session_id: str, content: str) -> str:
        if not self._executor:
            raise RuntimeError("Executor not registered")
        result = await self._executor(session_id, content)
        return result.result if hasattr(result, 'result') else str(result)
    
    def start_all(self):
        for name, plugin in self.plugins.items():
            if plugin.enabled:
                try:
                    plugin.start()
                    logger.info(f"Plugin [{name}] started")
                except Exception as e:
                    logger.error(f"Failed to start plugin {name}: {e}")
    
    def stop_all(self):
        for name, plugin in self.plugins.items():
            try:
                plugin.stop()
                logger.info(f"Plugin [{name}] stopped")
            except Exception as e:
                logger.error(f"Failed to stop plugin {name}: {e}")
    
    def get_plugin(self, name: str) -> Optional[BasePlugin]:
        return self.plugins.get(name)
    
    def list_plugins(self) -> List[Dict[str, Any]]:
        return [p.get_info() for p in self.plugins.values()]
    
    def enable_plugin(self, name: str) -> bool:
        plugin = self.plugins.get(name)
        if plugin:
            plugin.enabled = True
            return True
        return False
    
    def disable_plugin(self, name: str) -> bool:
        plugin = self.plugins.get(name)
        if plugin:
            plugin.enabled = False
            return True
        return False