import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Any

from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

logger = logging.getLogger("agent")

# MCP 连接配置
MCP_CONNECT_TIMEOUT = 30  # 连接超时（秒）
MCP_RECONNECT_DELAY = 5  # 重连延迟（秒）
MCP_MAX_RECONNECT_ATTEMPTS = 3  # 最大重连次数
MCP_HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）

# 配置 env 中的 ${VAR} 占位符(真实凭证不入版本库,经进程环境注入子进程)
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def resolve_env_values(env: dict[str, Any]) -> dict[str, str]:
    """把配置中的 ${VAR} 占位符解析为进程环境变量。

    未定义的变量不写入结果, 以便子进程继续继承父进程环境(而不是被空串覆盖)。
    """
    resolved: dict[str, str] = {}
    for key, value in (env or {}).items():
        if not isinstance(value, str):
            continue
        match = _PLACEHOLDER_RE.match(value.strip())
        if match:
            actual = os.environ.get(match.group(1))
            if actual is not None:
                resolved[key] = actual
            continue
        resolved[key] = value
    return resolved


class MCPServerConnection:
    """MCP服务器连接管理"""

    def __init__(self, name: str, config: dict[str, Any], base_dir: str = ""):
        self.name = name
        self.config = config
        self.base_dir = base_dir
        self.session: ClientSession | None = None
        self._conn_task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._close_requested: asyncio.Event | None = None
        self._server_params: StdioServerParameters | None = None
        self.tool_defs: list[dict[str, Any]] = []
        self._connected = False
        self._reconnect_attempts = 0
        self._health_check_task: asyncio.Task | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected and self.session is not None

    async def connect(self, timeout: int = MCP_CONNECT_TIMEOUT) -> bool:
        """连接MCP服务器

        连接(stdio 子进程 + ClientSession)在专属连接任务内建立并保持, __aexit__ 也在
        同一任务内执行: anyio 的 cancel scope 要求进出同任务(跨任务退出会 RuntimeError),
        而连接任务不接收调用方取消, 关闭时的在飞回调(stdio 子进程收尾)不会被取消打断。
        """
        if self._conn_task is not None and not self._conn_task.done():
            await self.close()

        command = self.config.get("command", "python")
        args = self.config.get("args", [])
        env = resolve_env_values(self.config.get("env", {}))

        resolved_args = [os.path.join(
            self.base_dir, a) if not os.path.isabs(a) else a for a in args]

        merged_env = dict(os.environ)
        merged_env["PYTHONUNBUFFERED"] = "1"
        merged_env.update(env)

        self._server_params = StdioServerParameters(
            command=command,
            args=resolved_args,
            env=merged_env
        )
        self._close_requested = asyncio.Event()
        self._ready = asyncio.get_running_loop().create_future()
        self._conn_task = asyncio.create_task(self._session_main(timeout))

        try:
            return await self._ready
        except asyncio.CancelledError:
            # 调用方被取消: 仍请求连接任务优雅收尾并等它完成, 再继续传播取消
            await self.close()
            raise

    async def _session_main(self, timeout: int) -> None:
        """专属连接任务: 在自身任务内进出 AsyncExitStack, 停放直到 close() 请求收尾。"""
        from contextlib import AsyncExitStack

        ready = self._ready
        close_requested = self._close_requested
        try:
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(timeout):
                        stdio_transport = await stack.enter_async_context(
                            stdio_client(self._server_params, errlog=subprocess.DEVNULL),
                        )
                        session = await stack.enter_async_context(
                            ClientSession(stdio_transport[0], stdio_transport[1])
                        )
                        await session.initialize()
                        mcp_tools = await session.list_tools()
                except asyncio.CancelledError:
                    if ready is not None and not ready.done():
                        ready.set_result(False)
                    raise
                except Exception as e:
                    logger.error(f"✗ MCP [{self.name}] 连接失败: {e}")
                    if ready is not None and not ready.done():
                        ready.set_result(False)
                    return

                self.session = session
                self.tool_defs = [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description or "",
                            "parameters": t.input_schema
                        }
                    }
                    for t in mcp_tools.tools
                ]
                self._connected = True
                self._reconnect_attempts = 0
                if ready is not None and not ready.done():
                    ready.set_result(True)

                if close_requested is not None:
                    await close_requested.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            logger.debug(f"MCP [{self.name}] 连接收尾异常(可忽略): {e}")
        finally:
            self.session = None
            self._connected = False

    async def _safe_exit_stack_cleanup(self):
        """安全清理连接(独立任务 + shielded 等待)

        unwind 在专属连接任务内执行(anyio cancel scope 进出同任务), 该任务不被取消,
        shield 只保护等待方: 调用方反复取消时循环等待到 task.done(), 再取回结果避免
        "exception never retrieved"。这样在飞回调(v2 stdio 子进程收尾)完整跑完,
        stdio 句柄不会因取消而漏清理; 等待期间取消不向调用方传播, 保证关闭流程走完。
        """
        task = self._conn_task
        self._conn_task = None
        if task is None:
            return
        if self._close_requested is not None:
            self._close_requested.set()
        self._connected = False

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # 等待方被取消: 连接任务不受影响, 继续等它收尾
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"MCP [{self.name}] 连接收尾回调异常(可忽略): {e}")
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
            self._reconnect_attempts = 0
            logger.info(f"✓ MCP [{self.name}] 重连成功")
        else:
            logger.warning(f"✗ MCP [{self.name}] 重连失败")

        return success

    async def health_check(self) -> bool:
        """健康检查"""
        if not self.is_connected or not self.session:
            return False

        try:
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

    async def call_tool(self, name: str, args: dict) -> str:
        """调用工具"""
        if not self.session or not self._connected:
            return "MCP未连接"

        try:
            result = await asyncio.wait_for(
                self.session.call_tool(name, args),
                timeout=60
            )
            parts = []
            for item in getattr(result, 'content', None) or []:
                text = getattr(item, 'text', None)
                if text is not None:
                    parts.append(text)
                elif isinstance(item, str):
                    parts.append(item)
            if parts:
                return "\n".join(parts)
            structured = getattr(result, 'structured_content', None)
            if structured is not None:
                return json.dumps(structured, ensure_ascii=False, default=str)
            if getattr(result, 'is_error', False):
                return "执行失败"
            return "执行成功"
        except asyncio.TimeoutError:
            logger.error(f"MCP [{self.name}] 工具调用超时: {name}")
            return "执行失败: 工具调用超时"
        except MCPError as e:
            logger.error(f"MCP [{self.name}] 工具调用失败: {name}, MCPError({e.code}): {e}")
            if e.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT):
                self._connected = False
            return f"执行失败: {e}"
        except Exception as e:
            logger.error(f"MCP [{self.name}] 工具调用失败: {name}, {type(e).__name__}: {e}")
            if isinstance(e, (ConnectionError, OSError, BrokenPipeError)):
                self._connected = False
            return f"执行失败: {e}"


class MCPManager:
    """MCP服务器管理器"""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path
        self.servers: dict[str, MCPServerConnection] = {}
        self.tool_defs: list[dict[str, Any]] = []
        self._tool_to_server: dict[str, str] = {}
        self._config_data: list[dict[str, Any]] = []
        self._health_check_task: asyncio.Task | None = None
        self._closing = False

    def load_config(self) -> list[dict[str, Any]]:
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

    def _get_server_config(self, name: str) -> dict[str, Any] | None:
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
        self._closing = True
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
        logger.debug("MCP 健康检查任务已启动")

    def stop_health_check(self):
        """停止健康检查任务"""
        if self._health_check_task:
            self._health_check_task.cancel()
            self._health_check_task = None
            logger.debug("MCP 健康检查任务已停止")

    async def _health_check_loop(self):
        """定期健康检查"""
        while True:
            try:
                await asyncio.sleep(MCP_HEALTH_CHECK_INTERVAL)
            except asyncio.CancelledError:
                logger.info("MCP 健康检查循环被取消")
                return
            try:
                await self._check_all_servers()
            except asyncio.CancelledError:
                logger.info("MCP 健康检查被取消")
                return
            except Exception as e:
                logger.error(f"MCP 健康检查失败: {e}")

    async def _check_all_servers(self):
        """检查所有服务器健康状态"""
        if self._closing:
            return
        for name, server in list(self.servers.items()):
            if self._closing:
                return
            if not await server.health_check():
                logger.warning(f"MCP [{name}] 健康检查失败，尝试重连")
                if not await server.reconnect():
                    logger.error(f"MCP [{name}] 重连失败")
                else:
                    self._refresh_tool_defs()

    def has_tool(self, name: str) -> bool:
        """检查是否有指定工具"""
        return name in self._tool_to_server

    async def call_tool(self, name: str, args: dict) -> str:
        """调用MCP工具"""
        server_name = self._tool_to_server.get(name)
        if not server_name:
            return f"工具 {name} 未找到"

        server = self.servers.get(server_name)
        if not server:
            return f"MCP服务 {server_name} 未连接"

        if not server.is_connected:
            logger.warning(f"MCP [{server_name}] 未连接，尝试重连")
            if not await server.reconnect():
                return f"MCP [{server_name}] 重连失败，无法调用工具 {name}"

        return await server.call_tool(name, args)

    def list_servers(self) -> list[dict[str, Any]]:
        """列出所有MCP服务器"""
        return [
            {
                "name": name,
                "tools": len(s.tool_defs),
                "connected": s.is_connected
            }
            for name, s in self.servers.items()
        ]

    async def connect_server(self, config: dict[str, Any]) -> bool:
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
            logger.debug(f"✓ MCP服务 [{name}] 已动态连接，加载 {len(server.tool_defs)} 个工具")
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

    async def reload_all(self) -> dict[str, bool]:
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
