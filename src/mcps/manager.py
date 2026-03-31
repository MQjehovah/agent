import os
import json
import logging
import asyncio
from typing import Optional, List, Dict, Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger("agent")

# MCP 连接配置
MCP_CONNECT_TIMEOUT = 30  # 连接超时（秒）
MCP_RECONNECT_DELAY = 5  # 重连延迟（秒）
MCP_MAX_RECONNECT_ATTEMPTS = 3  # 最大重连次数
MCP_HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）


class MCPServerConnection:
    """MCP服务器连接管理"""

    def __init__(self, name: str, config: Dict[str, Any], base_dir: str = ""):
        self.name = name
        self.config = config
        self.base_dir = base_dir
        self.session: Optional[ClientSession] = None
        self._exit_stack = None
        self.tool_defs: List[Dict[str, Any]] = []
        self._connected = False
        self._reconnect_attempts = 0
        self._health_check_task: Optional[asyncio.Task] = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self.session is not None

    async def connect(self, timeout: int = MCP_CONNECT_TIMEOUT) -> bool:
        """连接MCP服务器"""
        from contextlib import AsyncExitStack

        command = self.config.get("command", "python")
        args = self.config.get("args", [])
        env = self.config.get("env", {})

        resolved_args = [os.path.join(
            self.base_dir, a) if not os.path.isabs(a) else a for a in args]

        merged_env = dict(os.environ)
        merged_env.update(env)

        server_params = StdioServerParameters(
            command=command,
            args=resolved_args,
            env=merged_env
        )

        try:
            self._exit_stack = AsyncExitStack()
            await asyncio.wait_for(
                self._exit_stack.__aenter__(),
                timeout=timeout
            )

            try:
                stdio_transport = await asyncio.wait_for(
                    self._exit_stack.enter_async_context(stdio_client(server_params)),
                    timeout=timeout
                )
            except (asyncio.TimeoutError, asyncio.CancelledError) as e:
                logger.error(f"✗ MCP [{self.name}] 连接超时或被取消: {e}")
                await self._safe_exit_stack_cleanup()
                return False

            self.session = await self._exit_stack.enter_async_context(
                ClientSession(stdio_transport[0], stdio_transport[1])
            )
            await self.session.initialize()

            mcp_tools = await self.session.list_tools()
            self.tool_defs = []
            for t in mcp_tools.tools:
                self.tool_defs.append({
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema
                    }
                })

            self._connected = True
            self._reconnect_attempts = 0
            logger.info(f"✓ MCP [{self.name}] 已连接，加载 {len(self.tool_defs)} 个工具")
            return True

        except asyncio.TimeoutError:
            logger.error(f"✗ MCP [{self.name}] 连接超时")
            await self._safe_exit_stack_cleanup()
            return False
        except Exception as e:
            logger.error(f"✗ MCP [{self.name}] 连接失败: {e}")
            await self._safe_exit_stack_cleanup()
            return False

    async def _safe_exit_stack_cleanup(self):
        """安全清理 ExitStack"""
        if self._exit_stack:
            try:
                await self._exit_stack.__aexit__(None, None, None)
            except (RuntimeError, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.debug(f"MCP [{self.name}] ExitStack 清理时出错: {e}")
            finally:
                self._exit_stack = None
                self.session = None
                self._connected = False

    async def reconnect(self) -> bool:
        """重连MCP服务器"""
        if self._reconnect_attempts >= MCP_MAX_RECONNECT_ATTEMPTS:
            logger.error(f"✗ MCP [{self.name}] 已达最大重连次数")
            return False

        self._reconnect_attempts += 1
        logger.info(f"MCP [{self.name}] 尝试重连 ({self._reconnect_attempts}/{MCP_MAX_RECONNECT_ATTEMPTS})...")

        await self.close()
        await asyncio.sleep(MCP_RECONNECT_DELAY)

        success = await self.connect()
        if success:
            logger.info(f"✓ MCP [{self.name}] 重连成功")
        else:
            logger.warning(f"✗ MCP [{self.name}] 重连失败")

        return success

    async def health_check(self) -> bool:
        """健康检查"""
        if not self.is_connected or not self.session:
            return False

        try:
            # 尝试列出工具来验证连接
            await asyncio.wait_for(
                self.session.list_tools(),
                timeout=10
            )
            return True
        except Exception as e:
            logger.warning(f"MCP [{self.name}] 健康检查失败: {e}")
            self._connected = False
            return False

    async def close(self):
        """关闭连接"""
        self._connected = False
        if self._health_check_task:
            self._health_check_task.cancel()
            self._health_check_task = None

        await self._safe_exit_stack_cleanup()
        logger.debug(f"MCP [{self.name}] 连接已关闭")

    async def call_tool(self, name: str, args: Dict) -> str:
        """调用工具"""
        if not self.session or not self._connected:
            return "MCP未连接"

        try:
            result = await asyncio.wait_for(
                self.session.call_tool(name, args),
                timeout=60
            )
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
        except asyncio.TimeoutError:
            logger.error(f"MCP [{self.name}] 工具调用超时: {name}")
            self._connected = False
            return f"执行失败: 工具调用超时"
        except Exception as e:
            logger.error(f"MCP [{self.name}] 工具调用失败: {name}, 错误: {e}")
            return f"执行失败: {e}"


class MCPManager:
    """MCP服务器管理器"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.servers: Dict[str, MCPServerConnection] = {}
        self.tool_defs: List[Dict[str, Any]] = []
        self._tool_to_server: Dict[str, str] = {}
        self._config_data: List[Dict[str, Any]] = []
        self._health_check_task: Optional[asyncio.Task] = None

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
        base_dir = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))

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
        if self._health_check_task:
            self._health_check_task.cancel()
            self._health_check_task = None

        for server in list(reversed(self.servers.values())):
            await server.close()

    def start_health_check(self):
        """启动健康检查任务"""
        if self._health_check_task:
            return
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        logger.info("MCP 健康检查任务已启动")

    def stop_health_check(self):
        """停止健康检查任务"""
        if self._health_check_task:
            self._health_check_task.cancel()
            self._health_check_task = None
            logger.info("MCP 健康检查任务已停止")

    async def _health_check_loop(self):
        """定期健康检查"""
        while True:
            await asyncio.sleep(MCP_HEALTH_CHECK_INTERVAL)
            try:
                await self._check_all_servers()
            except Exception as e:
                logger.error(f"MCP 健康检查失败: {e}")

    async def _check_all_servers(self):
        """检查所有服务器健康状态"""
        for name, server in list(self.servers.items()):
            if not await server.health_check():
                logger.warning(f"MCP [{name}] 健康检查失败，尝试重连")
                if not await server.reconnect():
                    logger.error(f"MCP [{name}] 重连失败")
                else:
                    self._refresh_tool_defs()

    def has_tool(self, name: str) -> bool:
        """检查是否有指定工具"""
        return name in self._tool_to_server

    async def call_tool(self, name: str, args: Dict) -> str:
        """调用MCP工具"""
        server_name = self._tool_to_server.get(name)
        if not server_name:
            return f"工具 {name} 未找到"

        server = self.servers.get(server_name)
        if server:
            # 如果服务器未连接，尝试重连
            if not server.is_connected:
                logger.warning(f"MCP [{server_name}] 未连接，尝试重连")
                await server.reconnect()

            return await server.call_tool(name, args)
        return f"MCP服务 {server_name} 未连接"

    def list_servers(self) -> List[Dict[str, Any]]:
        """列出所有MCP服务器"""
        return [
            {
                "name": name,
                "tools": len(s.tool_defs),
                "connected": s.is_connected
            }
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

        base_dir = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        server = MCPServerConnection(name, config, base_dir)
        success = await server.connect()
        if success:
            self.servers[name] = server
            self._refresh_tool_defs()
            logger.info(f"✓ MCP服务 [{name}] 已动态连接，加载 {len(server.tool_defs)} 个工具")
            return True
        else:
            logger.warning(f"✗ MCP服务 [{name}] 动态连接失败")
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
