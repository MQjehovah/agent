import os
import json
import logging
from typing import Optional, List, Dict, Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("agent")


class MCPServerConnection:
    """MCP服务器连接管理"""
    
    def __init__(self, name: str, config: Dict[str, Any], base_dir: str = ""):
        self.name = name
        self.config = config
        self.base_dir = base_dir
        self.session: Optional[ClientSession] = None
        self._exit_stack = None
        self.tool_defs: List[Dict[str, Any]] = []

    async def connect(self) -> bool:
        """连接MCP服务器"""
        from contextlib import AsyncExitStack
        import asyncio

        command = self.config.get("command", "python")
        args = self.config.get("args", [])
        env = self.config.get("env", {})

        resolved_args = [os.path.join(self.base_dir, a) if not os.path.isabs(a) else a for a in args]

        merged_env = dict(os.environ)
        merged_env.update(env)

        server_params = StdioServerParameters(
            command=command,
            args=resolved_args,
            env=merged_env
        )

        try:
            self._exit_stack = AsyncExitStack()
            await self._exit_stack.__aenter__()

            try:
                stdio_transport = await self._exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
            except asyncio.CancelledError:
                logger.error(f"✗ MCP [{self.name}] 连接被取消（可能是超时或进程启动失败）")
                await self._exit_stack.__aexit__(None, None, None)
                self._exit_stack = None
                return False

            self.session = await self._exit_stack.enter_async_context(
                ClientSession(stdio_transport[0], stdio_transport[1])
            )
            await self.session.initialize()

            mcp_tools = await self.session.list_tools()
            for t in mcp_tools.tools:
                self.tool_defs.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema
                    }
                })

            logger.info(f"✓ MCP [{self.name}] 已加载 {len(mcp_tools.tools)} 个工具")
            return True
        except Exception as e:
            logger.error(f"✗ MCP [{self.name}] 连接失败: {e}")
            if self._exit_stack:
                try:
                    await self._exit_stack.__aexit__(None, None, None)
                except:
                    pass
                self._exit_stack = None
            return False

    async def close(self):
        """关闭连接"""
        if self._exit_stack:
            await self._exit_stack.__aexit__(None, None, None)

    async def call_tool(self, name: str, args: Dict) -> str:
        """调用工具"""
        if not self.session:
            return "MCP未连接"

        try:
            result = await self.session.call_tool(name, args)
            if hasattr(result, 'content') and result.content:
                parts = []
                for item in result.content:
                    text = getattr(item, 'text', None)
                    if text is not None:
                        parts.append(text)
                    elif isinstance(item, str):
                        parts.append(item)
                return "\n".join(parts)
            return "执行成功"
        except Exception as e:
            return f"执行失败: {e}"


class MCPManager:
    """MCP服务器管理器"""
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.servers: Dict[str, MCPServerConnection] = {}
        self.tool_defs: List[Dict[str, Any]] = []
        self._tool_to_server: Dict[str, str] = {}
        self._config_data: List[Dict[str, Any]] = []

    def load_config(self) -> List[Dict[str, Any]]:
        """加载MCP配置文件"""
        if not self.config_path or not os.path.exists(self.config_path):
            logger.warning(f"MCP配置文件不存在: {self.config_path}")
            return []

        with open(self.config_path, encoding="utf-8") as f:
            configs = json.load(f)

        enabled = [c for c in configs if c.get("enabled", True)]
        self._config_data = configs
        logger.info(f"发现 {len(enabled)} 个启用的MCP服务")
        return enabled

    def _get_server_config(self, name: str) -> Optional[Dict[str, Any]]:
        """获取指定服务器的配置"""
        for config in self._config_data:
            if config.get("name") == name:
                return config
        return None

    def _refresh_tool_defs(self) -> None:
        """刷新工具定义列表"""
        self.tool_defs = []
        self._tool_to_server = {}
        for name, server in self.servers.items():
            self.tool_defs.extend(server.tool_defs)
            for tool_def in server.tool_defs:
                tool_name = tool_def["function"]["name"]
                self._tool_to_server[tool_name] = name

    async def connect(self):
        """连接所有MCP服务器"""
        configs = self.load_config()
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        for config in configs:
            name = config.get("name", "unnamed")
            server = MCPServerConnection(name, config, base_dir)
            success = await server.connect()
            if success:
                self.servers[name] = server
                self.tool_defs.extend(server.tool_defs)
                for tool_def in server.tool_defs:
                    tool_name = tool_def["function"]["name"]
                    self._tool_to_server[tool_name] = name

        logger.info(f"✓ 共加载 {len(self.tool_defs)} 个MCP工具")

    async def close(self):
        """关闭所有MCP连接"""
        for server in self.servers.values():
            await server.close()

    async def call_tool(self, name: str, args: Dict) -> str:
        """调用MCP工具"""
        server_name = self._tool_to_server.get(name)
        if not server_name:
            return f"工具 {name} 未找到"

        server = self.servers.get(server_name)
        if server:
            return await server.call_tool(name, args)
        return f"MCP服务 {server_name} 未连接"

    def list_servers(self) -> List[Dict[str, Any]]:
        """列出所有MCP服务器"""
        return [
            {"name": name, "tools": len(s.tool_defs)}
            for name, s in self.servers.items()
        ]

    async def connect_server(self, config: Dict[str, Any]) -> bool:
        """动态连接MCP服务器
        
        Args:
            config: MCP服务器配置
            
        Returns:
            是否连接成功
        """
        name = config.get("name", "unnamed")
        if name in self.servers:
            await self.disconnect_server(name)

        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        server = MCPServerConnection(name, config, base_dir)
        success = await server.connect()
        if success:
            self.servers[name] = server
            self._refresh_tool_defs()
            logger.info(f"✓ MCP服务 [{name}] 已动态连接，加载 {len(server.tool_defs)} 个工具")
            return True
        else:
            logger.error(f"✗ MCP服务 [{name}] 动态连接失败")
            return False

    async def disconnect_server(self, name: str) -> bool:
        """断开MCP服务器
        
        Args:
            name: 服务器名称
            
        Returns:
            是否断开成功
        """
        if name not in self.servers:
            logger.warning(f"MCP服务 [{name}] 未连接")
            return False

        server = self.servers.pop(name)
        await server.close()
        self._refresh_tool_defs()
        logger.info(f"✓ MCP服务 [{name}] 已断开连接")
        return True

    async def reload_server(self, name: str) -> bool:
        """重载MCP服务器
        
        Args:
            name: 服务器名称
            
        Returns:
            是否重载成功
        """
        if name in self.servers:
            await self.disconnect_server(name)

        config = self._get_server_config(name)
        if not config:
            logger.error(f"未找到MCP服务 [{name}] 的配置")
            return False

        return await self.connect_server(config)

    async def reload_all(self) -> Dict[str, bool]:
        """重载所有MCP服务器
        
        Returns:
            各服务器的重载结果
        """
        results = {}
        for name in list(self.servers.keys()):
            await self.disconnect_server(name)

        configs = self.load_config()
        for config in configs:
            name = config.get("name", "unnamed")
            if not config.get("enabled", True):
                results[name] = True
                continue
            results[name] = await self.connect_server(config)

        success_count = sum(1 for v in results.values() if v)
        logger.info(f"重载完成: {success_count}/{len(results)} 个服务成功")
        return results