import asyncio
import json
import logging
import os
import re
import subprocess
import weakref
from typing import Any, Literal, cast

from mcp import ClientSession, MCPError, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

logger = logging.getLogger("agent")

# MCP 工具风险级别(注解映射 + risk_overrides 的结果域)
McpRisk = Literal["read", "write", "destructive", "unknown"]
MCP_RISK_LEVELS: tuple[str, ...] = ("read", "write", "destructive", "unknown")

# MCP 连接配置（默认值；可在 config/mcp_servers.json 中按 server 覆盖）
MCP_CONNECT_TIMEOUT = 30  # 连接超时（秒）
MCP_TOOL_TIMEOUT = 60  # 工具调用超时（秒）
MCP_RECONNECT_DELAY = 5  # 重连延迟（秒）
MCP_MAX_RECONNECT_ATTEMPTS = 3  # 最大重连次数
MCP_MAX_CONCURRENCY = 4  # 每 server 工具调用最大并发
MCP_HEALTH_CHECK_INTERVAL = 60  # 健康检查间隔（秒）
MCP_LAST_ERROR_MAX_LEN = 200  # last_error 摘要最大字符数
MCP_MAX_EXPOSED_NAME_LEN = 64  # LLM 暴露工具名最大长度
MCP_EXPOSED_NAME_INVALID_RE = re.compile(r"[^a-zA-Z0-9_-]")

# 实例注册表：聚合 root agent / worker 池等全部 MCPManager 的运行状态。
# 弱引用 + close() 注销，避免测试与生命周期泄漏。
_MANAGER_REGISTRY: "weakref.WeakSet[MCPManager]" = weakref.WeakSet()

# 配置 env 中的 ${VAR} 占位符(真实凭证不入版本库,经进程环境注入子进程)
_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def _parse_positive_number(raw: Any, default: float) -> tuple[float, bool]:
    """解析 >0 的数值配置（bool 视为非法）；返回 (值, 是否合法)。"""
    if raw is None:
        return default, True
    if isinstance(raw, bool):
        return default, False
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default, False
    if value <= 0:
        return default, False
    return value, True


def _parse_int_at_least(raw: Any, default: int, minimum: int) -> tuple[int, bool]:
    """解析 >= minimum 的整数配置（bool 视为非法）；返回 (值, 是否合法)。"""
    if raw is None:
        return default, True
    if isinstance(raw, bool):
        return default, False
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, False
    if value < minimum:
        return default, False
    return value, True


def _sanitize_exposed_name(name: str) -> str:
    """把工具名清洗到 ^[a-zA-Z0-9_-]{1,64}$（非法字符替换为 _）。"""
    cleaned = MCP_EXPOSED_NAME_INVALID_RE.sub("_", str(name or ""))[:MCP_MAX_EXPOSED_NAME_LEN]
    return cleaned or "tool"


def _annotation_flag(annotations: Any, snake: str, camel: str) -> bool | None:
    """读取注解布尔字段(兼容 SDK v2 snake_case 对象/dict 与 camelCase 旧字段名)。"""
    if annotations is None:
        return None
    if isinstance(annotations, dict):
        value = annotations.get(snake, annotations.get(camel))
    else:
        value = getattr(annotations, snake, None)
        if value is None:
            value = getattr(annotations, camel, None)
    return value if isinstance(value, bool) else None


def classify_tool_risk(annotations: Any) -> str:
    """把 MCP 工具注解(ToolAnnotations)映射为风险级别。

    readOnlyHint is True → 'read'; destructiveHint is True → 'destructive';
    有 annotations 但非只读(或 readOnlyHint=False) → 'write'; 无 annotations → 'unknown'。
    """
    if annotations is None:
        return "unknown"
    if _annotation_flag(annotations, "read_only_hint", "readOnlyHint") is True:
        return "read"
    if _annotation_flag(annotations, "destructive_hint", "destructiveHint") is True:
        return "destructive"
    return "write"


def format_tool_result(result: Any) -> str:
    """MCP 工具返回内容 → 文本(内容块拼接 / 结构化 JSON / 成败兜底)。

    本地 stdio 与平台轨共用, 保证两条链路的输出文案一致。
    """
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
        self._tool_risks: dict[str, str] = {}
        self._connected = False
        self._reconnect_attempts = 0
        self._health_check_task: asyncio.Task | None = None
        self._last_error = ""

        # 每 server 可配（非法值回退默认并告警）
        self.timeout_seconds = self._config_number("timeout_seconds", MCP_TOOL_TIMEOUT)
        self.connect_timeout_seconds = self._config_number("connect_timeout_seconds", MCP_CONNECT_TIMEOUT)
        self.max_reconnect_attempts = self._config_int("max_reconnect_attempts", MCP_MAX_RECONNECT_ATTEMPTS, minimum=0)
        self.max_concurrency = self._config_int("max_concurrency", MCP_MAX_CONCURRENCY, minimum=1)
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        # 工具级 risk_overrides(原始工具名匹配, 覆盖注解)
        self._risk_overrides = self._config_risk_overrides()

    def _config_risk_overrides(self) -> dict[str, str]:
        """解析 risk_overrides(原始工具名 → read/write/destructive), 非法值忽略并告警。"""
        raw = self.config.get("risk_overrides")
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            logger.warning(f"MCP [{self.name}] 配置 risk_overrides 非法(需为对象), 已忽略")
            return {}
        overrides: dict[str, str] = {}
        for tool_name, risk in raw.items():
            level = str(risk).strip().lower()
            if level in ("read", "write", "destructive"):
                overrides[str(tool_name)] = level
            else:
                logger.warning(f"MCP [{self.name}] risk_overrides[{tool_name}]={risk!r} 非法, 已忽略")
        return overrides

    def risk_of(self, raw_name: str) -> str:
        """原始工具名 → 风险级别(注解 + risk_overrides 覆盖); 未知工具默认 unknown。"""
        return self._tool_risks.get(str(raw_name), "unknown")

    def _config_number(self, key: str, default: float) -> float:
        value, ok = _parse_positive_number(self.config.get(key), default)
        if not ok:
            logger.warning(f"MCP [{self.name}] 配置 {key}={self.config.get(key)!r} 非法, 回退默认 {default}")
        return value

    def _config_int(self, key: str, default: int, *, minimum: int) -> int:
        value, ok = _parse_int_at_least(self.config.get(key), default, minimum)
        if not ok:
            logger.warning(f"MCP [{self.name}] 配置 {key}={self.config.get(key)!r} 非法, 回退默认 {default}")
        return value

    @property
    def is_connected(self) -> bool:
        return self._connected and self.session is not None

    @property
    def connected(self) -> bool:
        """对外状态别名（is_connected）。"""
        return self.is_connected

    @property
    def reconnect_attempts(self) -> int:
        return self._reconnect_attempts

    @property
    def tool_count(self) -> int:
        return len(self.tool_defs)

    @property
    def last_error(self) -> str:
        return self._last_error

    def _set_last_error(self, message: str) -> None:
        self._last_error = (message or "")[:MCP_LAST_ERROR_MAX_LEN]

    def _clear_last_error(self) -> None:
        self._last_error = ""

    async def connect(self, timeout: float | None = None) -> bool:
        """连接MCP服务器

        连接(stdio 子进程 + ClientSession)在专属连接任务内建立并保持, __aexit__ 也在
        同一任务内执行: anyio 的 cancel scope 要求进出同任务(跨任务退出会 RuntimeError),
        而连接任务不接收调用方取消, 关闭时的在飞回调(stdio 子进程收尾)不会被取消打断。

        timeout 缺省时使用配置的 connect_timeout_seconds(默认 30s)。
        """
        if timeout is None:
            timeout = self.connect_timeout_seconds
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

    async def _session_main(self, timeout: float) -> None:
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
                    self._set_last_error(f"连接失败: {str(e) or type(e).__name__}")
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
                # 工具级风险: 注解 → read/write/destructive/unknown, risk_overrides 按原始名覆盖
                self._tool_risks = {
                    str(t.name): self._risk_overrides.get(str(t.name))
                    or classify_tool_risk(getattr(t, "annotations", None))
                    for t in mcp_tools.tools
                }
                self._connected = True
                self._reconnect_attempts = 0
                self._clear_last_error()
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
        if self._reconnect_attempts >= self.max_reconnect_attempts:
            logger.error(f"✗ MCP [{self.name}] 已达最大重连次数({self.max_reconnect_attempts})")
            return False

        self._reconnect_attempts += 1
        logger.info(f"MCP [{self.name}] 尝试重连 ({self._reconnect_attempts}/{self.max_reconnect_attempts})...")

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
            self._clear_last_error()
            return True
        except Exception as e:
            logger.warning(f"MCP [{self.name}] 健康检查失败: {e}")
            self._set_last_error(f"健康检查失败: {str(e) or type(e).__name__}")
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
        """调用工具

        受 max_concurrency 信号量约束（超限排队而非报错），超时用 timeout_seconds
        （默认 60s，可按 server 配置覆盖）。
        """
        if not self.session or not self._connected:
            return "MCP未连接"

        async with self._semaphore:
            try:
                result = await asyncio.wait_for(
                    self.session.call_tool(name, args),
                    timeout=self.timeout_seconds
                )
                return format_tool_result(result)
            except asyncio.TimeoutError:
                message = f"工具调用超时: {name}"
                logger.error(f"MCP [{self.name}] {message}")
                self._set_last_error(message)
                return "执行失败: 工具调用超时"
            except MCPError as e:
                logger.error(f"MCP [{self.name}] 工具调用失败: {name}, MCPError({e.code}): {e}")
                self._set_last_error(f"工具调用失败: {name}, MCPError({e.code}): {e}")
                if e.code in (CONNECTION_CLOSED, REQUEST_TIMEOUT):
                    self._connected = False
                return f"执行失败: {e}"
            except Exception as e:
                logger.error(f"MCP [{self.name}] 工具调用失败: {name}, {type(e).__name__}: {e}")
                self._set_last_error(f"工具调用失败: {name}, {type(e).__name__}: {e}")
                if isinstance(e, (ConnectionError, OSError, BrokenPipeError)):
                    self._connected = False
                return f"执行失败: {e}"


def _merge_platform_status(clients: list[Any]) -> dict[str, Any] | None:
    """聚合各实例的平台 MCP 轨状态(同名能力合并, 优先已连接实例); 无平台轨返回 None。"""
    if not clients:
        return None
    rows: dict[str, dict[str, Any]] = {}
    base_url = ""
    last_refresh = ""
    last_error = ""
    for client in clients:
        try:
            data = client.status()
        except Exception as e:
            logger.warning(f"聚合平台 MCP 状态失败: {e}")
            continue
        base_url = base_url or str(data.get("base_url") or "")
        last_refresh = max(last_refresh, str(data.get("last_refresh") or ""))
        last_error = last_error or str(data.get("last_error") or "")
        for row in data.get("servers") or []:
            name = str(row.get("name") or "")
            if not name:
                continue
            prev = rows.get(name)
            if prev is None:
                rows[name] = dict(row)
                continue
            pick = row if (row.get("connected") and not prev.get("connected")) else prev
            rows[name] = {
                **pick,
                "tools": max(int(prev.get("tools") or 0), int(row.get("tools") or 0)),
                "last_error": pick.get("last_error") or prev.get("last_error") or row.get("last_error") or "",
                "last_refresh": max(str(prev.get("last_refresh") or ""),
                                    str(row.get("last_refresh") or "")),
            }
    servers = [rows[name] for name in sorted(rows)]
    connected = sum(1 for row in servers if row.get("connected"))
    return {
        "enabled": True,
        "base_url": base_url,
        "last_refresh": last_refresh,
        "last_error": last_error,
        "servers": servers,
        "summary": {"connected": connected, "failed": len(servers) - connected,
                    "total": len(servers)},
    }


class MCPManager:
    """MCP服务器管理器"""

    def __init__(self, config_path: str | None = None):
        self.config_path = config_path
        self.servers: dict[str, MCPServerConnection] = {}
        self.tool_defs: list[dict[str, Any]] = []
        self._tool_to_server: dict[str, str] = {}
        self._exposed_to_server: dict[str, str] = {}
        self._exposed_to_raw: dict[str, str] = {}
        self._exposed_to_risk: dict[str, str] = {}
        self._reserved_names: set[str] = set()
        self._config_data: list[dict[str, Any]] = []
        self._health_check_task: asyncio.Task | None = None
        self._closing = False
        self._server_errors: dict[str, str] = {}
        # 平台 MCP 轨(市场能力): 与本地 server 组合, 接口同构; None=未启用
        self.platform: Any = None
        _MANAGER_REGISTRY.add(self)

    def attach_platform(self, platform: Any) -> None:
        """挂接平台 MCP 轨(市场能力), 工具/风险/分发与本地合并(本地优先)。"""
        self.platform = platform
        register = getattr(platform, "attach_manager", None)
        if callable(register):
            try:
                register(self)
            except Exception as e:
                logger.warning(f"平台 MCP 轨挂接 manager 失败(忽略): {e}")
        self._rebuild_tool_defs()

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

    def _remember_config(self, config: dict[str, Any]) -> None:
        """登记 server 配置(动态 connect_server 路径也纳入 status 聚合)。"""
        name = config.get("name", "unnamed")
        for i, existing in enumerate(self._config_data):
            if existing.get("name") == name:
                self._config_data[i] = config
                return
        self._config_data.append(config)

    def _rebuild_tool_defs(self) -> None:
        """重建暴露给 LLM 的工具列表, 对重名工具加 server 前缀去重。

        规则: 第一个出现的名字保持原名; 后续重名(跨 server 或与内置/已注册名冲突)改为
        `<server>__<raw>`, sanitize 到 ^[a-zA-Z0-9_-]{1,64}$, 仍冲突则追加 _2/_3...
        映射(_exposed_to_server/_exposed_to_raw/_tool_to_server)整体重建, 不残留。
        """
        self.tool_defs = []
        self._tool_to_server = {}
        self._exposed_to_server = {}
        self._exposed_to_raw = {}
        self._exposed_to_risk = {}
        used: set[str] = set(self._reserved_names)

        for server_name, server in self.servers.items():
            for tool_def in server.tool_defs:
                raw_name = str(tool_def.get("function", {}).get("name") or "")
                exposed = self._allocate_exposed_name(server_name, raw_name, used)
                used.add(exposed)
                self._tool_to_server[exposed] = server_name
                self._exposed_to_server[exposed] = server_name
                self._exposed_to_raw[exposed] = raw_name
                self._exposed_to_risk[exposed] = server.risk_of(raw_name)
                if exposed != raw_name:
                    logger.warning(
                        f"MCP [{server_name}] 工具重名: {raw_name} -> 暴露为 {exposed}（避免 LLM 工具名冲突）")
                self.tool_defs.append({
                    **tool_def,
                    "function": {**tool_def["function"], "name": exposed},
                })

        # 平台轨(市场 MCP): 暴露名自带 platform__ 前缀, 与本地/保留名冲突时本地优先(跳过)
        platform = self.platform
        if platform is not None:
            try:
                platform_defs = platform.tool_defs
            except Exception as e:
                platform_defs = []
                logger.warning(f"读取平台 MCP 工具表失败(忽略): {e}")
            for tool_def in platform_defs:
                exposed = str(tool_def.get("function", {}).get("name") or "")
                if not exposed:
                    continue
                if exposed in used:
                    logger.warning(f"平台 MCP 工具 {exposed} 与本地/保留工具重名, 本地优先, 已跳过")
                    continue
                used.add(exposed)
                self.tool_defs.append(tool_def)

    @staticmethod
    def _allocate_exposed_name(server_name: str, raw_name: str, used: set[str]) -> str:
        """为 raw 工具名分配唯一暴露名(首选原名, 冲突则 server 前缀, 再冲突加序号)。"""
        base = _sanitize_exposed_name(raw_name)
        if base not in used:
            return base
        prefixed = _sanitize_exposed_name(f"{server_name}__{raw_name}")
        if prefixed not in used:
            return prefixed
        index = 2
        while True:
            suffix = f"_{index}"
            candidate = prefixed[: MCP_MAX_EXPOSED_NAME_LEN - len(suffix)] + suffix
            if candidate not in used:
                return candidate
            index += 1

    def set_reserved_names(self, names: set[str] | list[str] | None) -> None:
        """登记非 MCP(内置/技能/插件)工具名; 变更时重建暴露名映射以避开冲突。"""
        new_names = {str(n) for n in (names or ()) if n}
        if new_names != self._reserved_names:
            self._reserved_names = new_names
            self._rebuild_tool_defs()

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
                self._server_errors.pop(name, None)
            else:
                # 故障隔离: 单 server 失败不阻塞其它, 记入失败清单
                self._server_errors[name] = server.last_error or "连接失败"

        self._rebuild_tool_defs()
        logger.info(f"✓ 共加载 {len(self.tool_defs)} 个MCP工具")

    async def close(self):
        """关闭所有MCP连接(含平台 MCP 轨: 停刷新任务并断开能力会话)"""
        self._closing = True
        _MANAGER_REGISTRY.discard(self)
        if self._health_check_task:
            self._health_check_task.cancel()
            self._health_check_task = None

        for server in list(reversed(self.servers.values())):
            await server.close()

        platform = self.platform
        self.platform = None
        if platform is not None:
            try:
                await platform.close()
            except Exception as e:
                logger.warning(f"平台 MCP 轨关闭失败(忽略): {e}")

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
                    self._rebuild_tool_defs()

    def has_tool(self, name: str) -> bool:
        """检查是否有指定工具(本地 server 优先, 平台轨兜底)。"""
        if name in self._tool_to_server:
            return True
        return bool(self.platform and self.platform.has_tool(name))

    def is_tool_available(self, name: str) -> bool:
        """暴露工具名当前是否可用(渐进披露注入前过滤用)。

        本地 MCP: 映射存在且对应 server 已连接(断开 server 的工具定义仍留在
        工具表里, 供重连恢复, 但不能注入给 LLM); 平台轨: 未连接能力不暴露
        (platform.has_tool 只反映已连接能力); 未知名字返回 False。
        """
        server_name = self._tool_to_server.get(name)
        if server_name:
            server = self.servers.get(server_name)
            return bool(server and server.is_connected)
        platform = self.platform
        return bool(platform is not None and platform.has_tool(name))

    def tool_risk(self, exposed_name: str) -> McpRisk | None:
        """暴露工具名 → 风险级别(read/write/destructive/unknown)。

        本地映射随 _rebuild_tool_defs 重建, 平台工具实时向平台轨查询;
        非 MCP 工具名返回 None(权限层据此区分)。
        """
        risk = self._exposed_to_risk.get(exposed_name)
        if risk is None and self.platform is not None:
            risk = self.platform.tool_risk(exposed_name)
        if risk is None:
            return None
        return cast(McpRisk, risk if risk in MCP_RISK_LEVELS else "unknown")

    def tool_server(self, exposed_name: str) -> str | None:
        """暴露工具名 → server 名(平台工具为 platform:{能力名}); 非 MCP 返回 None。"""
        server = self._exposed_to_server.get(exposed_name)
        if server is None and self.platform is not None:
            server = self.platform.tool_server(exposed_name)
        return server

    def tool_raw(self, exposed_name: str) -> str | None:
        """暴露工具名 → 服务端原始工具名; 非 MCP 工具名返回 None(审计记录用)。"""
        raw = self._exposed_to_raw.get(exposed_name)
        if raw is None and self.platform is not None:
            raw = self.platform.tool_raw(exposed_name)
        return raw

    async def call_tool(self, name: str, args: dict) -> str:
        """调用MCP工具(入参为 LLM 暴露名, 按映射解析回 server 与原始工具名; 本地优先)"""
        server_name = self._tool_to_server.get(name)
        if not server_name:
            if self.platform is not None and self.platform.has_tool(name):
                return await self.platform.call_tool(name, args)
            return f"工具 {name} 未找到"

        server = self.servers.get(server_name)
        if not server:
            return f"MCP服务 {server_name} 未连接"

        if not server.is_connected:
            logger.warning(f"MCP [{server_name}] 未连接，尝试重连")
            if not await server.reconnect():
                return f"MCP [{server_name}] 重连失败，无法调用工具 {name}"
            # 重连会重新 list_tools: server 侧工具可能增删/改名, 必须重建暴露映射,
            # 否则后续调用与 LLM 工具表使用的是重连前的旧映射(悬空/漏工具)。
            self._rebuild_tool_defs()

        raw_name = self._exposed_to_raw.get(name, name)
        return await server.call_tool(raw_name, args)

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

    def status(self) -> dict[str, Any]:
        """本实例各 server 状态与汇总(供 /healthz 与 /api/admin/mcp)。

        覆盖已配置但连接失败的 server(故障隔离清单), 状态含:
        name/enabled/connected/tools/last_error/reconnects/timeout_seconds/max_concurrency。
        """
        configs: dict[str, dict[str, Any]] = {}
        for config in self._config_data:
            configs.setdefault(config.get("name", "unnamed"), config)
        for name, server in self.servers.items():
            configs.setdefault(name, server.config)

        servers: list[dict[str, Any]] = []
        connected_count = 0
        failed_count = 0
        for name in sorted(configs):
            config = configs[name]
            conn = self.servers.get(name)
            connected = bool(conn and conn.is_connected)
            enabled = bool(config.get("enabled", True))
            if conn is not None:
                tools, reconnects, error = conn.tool_count, conn.reconnect_attempts, conn.last_error
            else:
                tools, reconnects, error = 0, 0, ""
            if not connected:
                error = error or self._server_errors.get(name, "")
            if enabled and not connected and not error:
                error = "未连接"
            if connected:
                connected_count += 1
            elif enabled:
                failed_count += 1
            timeout_seconds = conn.timeout_seconds if conn is not None else _parse_positive_number(
                config.get("timeout_seconds"), MCP_TOOL_TIMEOUT)[0]
            max_concurrency = conn.max_concurrency if conn is not None else _parse_int_at_least(
                config.get("max_concurrency"), MCP_MAX_CONCURRENCY, 1)[0]
            servers.append({
                "name": name,
                "enabled": enabled,
                "connected": connected,
                "tools": tools,
                "last_error": error,
                "reconnects": reconnects,
                "timeout_seconds": timeout_seconds,
                "max_concurrency": max_concurrency,
            })
        return {
            "servers": servers,
            "summary": {"connected": connected_count, "failed": failed_count, "total": len(servers)},
        }

    @staticmethod
    def status_all() -> dict[str, Any]:
        """聚合进程内全部实例(含 worker 池)的状态。

        本地 server 同名合并(优先已连接实例), 行内标 ``source=local``;
        平台 MCP 轨(市场能力)单独汇总到 ``platform`` 块, 每能力一行标 ``source=platform``
        并附 version/last_refresh; 无平台轨时为 None。
        """
        merged: dict[str, dict[str, Any]] = {}
        platform_clients: list[Any] = []
        for mgr in list(_MANAGER_REGISTRY):
            try:
                data = mgr.status()
            except Exception as e:
                logger.warning(f"聚合 MCP 状态失败: {e}")
                continue
            for row in data["servers"]:
                name = row["name"]
                prev = merged.get(name)
                if prev is None:
                    merged[name] = dict(row)
                    continue
                pick = row if (row["connected"] and not prev["connected"]) else prev
                merged[name] = {
                    **pick,
                    "enabled": prev["enabled"] or row["enabled"],
                    "tools": max(prev["tools"], row["tools"]),
                    "reconnects": max(prev["reconnects"], row["reconnects"]),
                    "last_error": pick["last_error"] or prev["last_error"] or row["last_error"],
                }
            platform = getattr(mgr, "platform", None)
            if platform is not None:
                platform_clients.append(platform)
        servers = [merged[name] for name in sorted(merged)]
        for row in servers:
            row.setdefault("source", "local")
        return {
            "servers": servers,
            "summary": {
                "connected": sum(1 for s in servers if s["connected"]),
                "failed": sum(1 for s in servers if s["enabled"] and not s["connected"]),
                "total": len(servers),
            },
            "platform": _merge_platform_status(platform_clients),
        }

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

        self._remember_config(config)
        base_dir = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        server = MCPServerConnection(name, config, base_dir)
        success = await server.connect()
        if success:
            self.servers[name] = server
            self._server_errors.pop(name, None)
            self._rebuild_tool_defs()
            logger.debug(f"✓ MCP服务 [{name}] 已动态连接，加载 {len(server.tool_defs)} 个工具")
            return True
        else:
            self._server_errors[name] = server.last_error or "连接失败"
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
        self._server_errors.pop(name, None)
        self._rebuild_tool_defs()
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
