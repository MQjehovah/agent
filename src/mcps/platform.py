"""市场平台 MCP 轨: 从市场能力目录 sync 拉取已发布 MCP 能力, 经能力网关直连。

与本地 stdio MCP(manager.py)并列: ``PlatformMCPClient`` 暴露与 ``MCPManager`` 相同的
查询/调用接口(tool_defs/has_tool/tool_risk/tool_server/tool_raw/call_tool/status),
由 ``MCPManager.attach_platform`` 组合后统一供 LLM 工具表、权限风险解析与 mcp_calls 审计。

契约(市场生产已验证):
- 目录: ``GET {MARKET_BASE_URL}/api/capabilities/sync`` (Bearer=市场服务令牌) → 数组;
  仅接 ``type=="mcp"`` 且 ``distribution in (remote,both)`` 的能力(local 网关会 403);
- 能力级网关: ``/api/mcp-gateway/relay/{name}/stream`` (Streamable HTTP, 同 Bearer),
  name 支持 ``name@version`` 钉版本; gateway 为 null 时同样可按名路由。

可用性: ``MARKET_BASE_URL`` 与 ``MARKET_SERVICE_TOKEN`` 齐备才启用; sync 失败仅告警并
保留既有连接; 单个能力连接/调用失败互不影响(连接分小批推进, 失败项快速退避重试);
last_error 经 ``unwrap_error`` 解包异常组后展示根因; 令牌不落日志与状态。
"""
import asyncio
import logging
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from .manager import (
    MCP_MAX_CONCURRENCY,
    MCP_RISK_LEVELS,
    _parse_positive_number,
    _sanitize_exposed_name,
    classify_tool_risk,
    format_tool_result,
)

logger = logging.getLogger("agent")

# 平台轨默认参数(env 可覆盖; 非法值回退默认并告警)
MARKET_DEFAULT_REFRESH_SECONDS = 300.0
MARKET_DEFAULT_TIMEOUT = 60.0
MARKET_EXPOSED_PREFIX = "platform__"
MARKET_DISTRIBUTIONS = ("remote", "both")
# 网关 split_cap_ref 仅把 X.Y.Z 当版本; 其它"版本"按整名处理, 故钉版本前先校验
MARKET_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# 连接压力控制: 启动时逐小批连接(批内并发 ≤2), 批间短暂停, 避免瞬时并发拉起上游进程
MARKET_CONNECT_BATCH_SIZE = 2
MARKET_CONNECT_BATCH_INTERVAL = 0.3
# 失败能力快速重试: 首次 30s, 之后指数翻倍, 封顶刷新周期(30→60→120→…)
MARKET_RETRY_BACKOFF_BASE = 30.0
# last_error 文案: 解包后异常消息保留 300 字, 整体上限再留前缀与类型名余量
MARKET_ERROR_MESSAGE_MAX_CHARS = 300
MARKET_LAST_ERROR_MAX_LEN = 400


def unwrap_error(exc: BaseException) -> BaseException:
    """递归解包异常组: 逐层取第一个子异常, 返回最内层普通异常。

    anyio/TaskGroup 会把真实失败包进 ``ExceptionGroup``(生产上导致 last_error
    只显示 "unhandled errors in a TaskGroup"), 解包后文案才能定位到根因。
    通过 ``exceptions`` 属性访问(兼容鸭子类型异常组); 空组/带环/非组异常均原样返回。
    """
    seen: set[int] = set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        children = getattr(current, "exceptions", None)
        if not isinstance(children, (tuple, list)) or not children:
            break
        seen.add(id(current))
        first = children[0]
        if not isinstance(first, BaseException):
            break
        current = first
    return current


def format_market_error(prefix: str, exc: BaseException) -> str:
    """异常(组) → last_error 文案 ``前缀: 类型: 消息前 300 字``。"""
    root = unwrap_error(exc)
    message = str(root)
    if len(message) > MARKET_ERROR_MESSAGE_MAX_CHARS:
        message = message[:MARKET_ERROR_MESSAGE_MAX_CHARS]
    return f"{prefix}: {type(root).__name__}: {message}"[:MARKET_LAST_ERROR_MAX_LEN]


def next_backoff(current: float, cap: float) -> float:
    """快速重试退避: 未开始(≤0)取 30s, 其后翻倍, 封顶 cap(刷新周期)。"""
    if cap <= 0:
        return 0.0
    if current <= 0:
        return min(MARKET_RETRY_BACKOFF_BASE, cap)
    return min(current * 2.0, cap)


@dataclass(frozen=True)
class PlatformMCPConfig:
    """平台轨配置(全部可选; base_url 与 service_token 齐备才启用)。"""

    base_url: str = ""
    service_token: str = ""
    refresh_seconds: float = MARKET_DEFAULT_REFRESH_SECONDS
    timeout: float = MARKET_DEFAULT_TIMEOUT

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.service_token)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PlatformMCPConfig":
        """从环境变量解析(缺省=关闭; 非法数值回退默认; 令牌不落日志)。"""
        source: Mapping[str, str] = os.environ if env is None else env
        base_url = str(source.get("MARKET_BASE_URL") or "").strip().rstrip("/")
        service_token = str(source.get("MARKET_SERVICE_TOKEN") or "").strip()
        refresh_seconds, refresh_ok = _parse_positive_number(
            source.get("MARKET_PLATFORM_REFRESH_SECONDS"), MARKET_DEFAULT_REFRESH_SECONDS)
        timeout, timeout_ok = _parse_positive_number(
            source.get("MARKET_PLATFORM_TIMEOUT"), MARKET_DEFAULT_TIMEOUT)
        if not refresh_ok:
            logger.warning(
                f"MARKET_PLATFORM_REFRESH_SECONDS={source.get('MARKET_PLATFORM_REFRESH_SECONDS')!r} 非法, "
                f"回退默认 {MARKET_DEFAULT_REFRESH_SECONDS:g}s")
        if not timeout_ok:
            logger.warning(
                f"MARKET_PLATFORM_TIMEOUT={source.get('MARKET_PLATFORM_TIMEOUT')!r} 非法, "
                f"回退默认 {MARKET_DEFAULT_TIMEOUT:g}s")
        if bool(base_url) != bool(service_token):
            logger.warning("MARKET_BASE_URL 与 MARKET_SERVICE_TOKEN 需同时配置, 平台 MCP 轨未启用")
        return cls(base_url=base_url, service_token=service_token,
                   refresh_seconds=refresh_seconds, timeout=timeout)


def parse_sync_capabilities(payload: Any) -> list[dict[str, Any]]:
    """sync 响应 → 可经网关直连的 MCP 能力(非法项跳过; distribution 缺省按 both)。"""
    if not isinstance(payload, list):
        return []
    caps: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip().lower() != "mcp":
            continue
        distribution = str(item.get("distribution") or "both").strip().lower()
        if distribution not in MARKET_DISTRIBUTIONS:
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        gateway = item.get("gateway")
        caps.append({
            "name": name,
            "version": str(item.get("version") or ""),
            "status": str(item.get("status") or ""),
            "distribution": distribution,
            "gateway": gateway if isinstance(gateway, dict) else None,
        })
    return caps


def exposed_tool_name(capability_name: str, tool_name: str) -> str:
    """平台工具暴露名 ``platform__{能力名}__{工具名}``(sanitize 到 LLM 工具名规范)。

    前缀与本地 server 隔离, 本地重名时由 MCPManager 按"本地优先"跳过平台项。
    """
    return _sanitize_exposed_name(f"{MARKET_EXPOSED_PREFIX}{capability_name}__{tool_name}")


def relay_stream_url(base_url: str, capability_name: str, *, version: str = "",
                     gateway: Mapping[str, Any] | None = None) -> str:
    """能力级网关 Streamable HTTP 端点。

    优先用目录 ``gateway.stream_url``(市场已 quote 的相对路径); 缺省按能力名路由,
    version 为合法 semver 时以 ``name@version`` 钉版本(保持工具集稳定; 非 semver
    会被网关当作整名解析, 故只在合法时钉)。
    """
    base = (base_url or "").rstrip("/")
    if gateway:
        stream_url = str(gateway.get("stream_url") or "").strip()
        if stream_url.startswith(("http://", "https://")):
            return stream_url
        if stream_url.startswith("/"):
            return f"{base}{stream_url}"
    ref = capability_name
    if version and MARKET_SEMVER_RE.match(version.strip()):
        ref = f"{capability_name}@{version.strip()}"
    return f"{base}/api/mcp-gateway/relay/{quote(ref, safe='@')}/stream"


@asynccontextmanager
async def default_session_opener(url: str, headers: dict[str, str]):
    """真实 MCP SDK Streamable HTTP 会话(连接/initialize 的限时由调用方负责)。"""
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    http_client = create_mcp_http_client(headers=headers)
    async with (
        http_client,
        streamable_http_client(url, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


class _CapabilityState:
    """单个平台能力的运行状态(会话/工具/最近错误/最近刷新)。"""

    def __init__(self, name: str, version: str, url: str):
        self.name = name
        self.version = version
        self.url = url
        self.session: Any = None
        self.tools: list[Any] = []
        self.connected = False
        self.last_error = ""
        self.last_refresh = ""
        self.backoff = 0.0  # 快速重试退避秒数(0=无待重试); 成功即清零
        self.retry_at = 0.0  # 下次快速重试时刻(注入时钟的读数)
        self._conn_task: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None
        self._close_requested: asyncio.Event | None = None
        self.semaphore = asyncio.Semaphore(MCP_MAX_CONCURRENCY)

    def set_last_error(self, message: str) -> None:
        self.last_error = (message or "")[:MARKET_LAST_ERROR_MAX_LEN]

    def clear_last_error(self) -> None:
        self.last_error = ""


class PlatformMCPClient:
    """平台 MCP 能力集合(接口与 MCPManager 对齐, 供其组合; 单实例=单 Agent 轨)。"""

    def __init__(self, config: PlatformMCPConfig, *,
                 transport: httpx.AsyncBaseTransport | None = None,
                 session_opener: Callable[[str, dict[str, str]], AbstractAsyncContextManager[Any]] | None = None,
                 now: Callable[[], float] | None = None,
                 sleeper: Callable[[float], Awaitable[None]] | None = None):
        self.config = config
        self._transport = transport
        self._session_opener = session_opener or default_session_opener
        self._now = now or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._caps: dict[str, _CapabilityState] = {}
        self._exposed_to_cap: dict[str, str] = {}
        self._exposed_to_raw: dict[str, str] = {}
        self._exposed_to_risk: dict[str, str] = {}
        self.tool_defs: list[dict[str, Any]] = []
        self.last_error = ""
        self.last_refresh = ""
        self._sync_client: httpx.AsyncClient | None = None
        self._refresh_task: asyncio.Task | None = None
        self._retry_task: asyncio.Task | None = None
        self._retry_wake = asyncio.Event()
        self._refresh_lock = asyncio.Lock()
        self._managers: weakref.WeakSet[Any] = weakref.WeakSet()
        self._closing = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动周期刷新任务(立即执行首轮; 非阻塞, sync 失败仅告警)。"""
        if self._closing:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        try:
            self._refresh_task = asyncio.get_running_loop().create_task(self._refresh_loop())
            logger.info(
                f"平台 MCP 轨已启动: {self.config.base_url}(每 {self.config.refresh_seconds:g}s 刷新)")
        except RuntimeError:
            logger.warning("平台 MCP 轨启动失败: start() 需在事件循环内调用")

    async def _refresh_loop(self) -> None:
        while not self._closing:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"平台 MCP 刷新异常(忽略): {e}")
            try:
                await asyncio.sleep(self.config.refresh_seconds)
            except asyncio.CancelledError:
                raise

    async def close(self) -> None:
        """停止刷新并关闭全部平台连接(幂等); 清空工具表与映射。"""
        self._closing = True
        task = self._refresh_task
        self._refresh_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"平台 MCP 刷新任务收尾异常(可忽略): {e}")
        retry = self._retry_task
        self._retry_task = None
        if retry is not None and not retry.done():
            retry.cancel()
            try:
                await retry
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug(f"平台 MCP 重试任务收尾异常(可忽略): {e}")
        for name in list(self._caps):
            await self._close_capability(name)
        self.tool_defs = []
        self._exposed_to_cap = {}
        self._exposed_to_raw = {}
        self._exposed_to_risk = {}
        client = self._sync_client
        self._sync_client = None
        if client is not None:
            try:
                await client.aclose()
            except Exception as e:
                logger.debug(f"平台 MCP sync 客户端收尾异常(可忽略): {e}")

    # ---------- 刷新 ----------

    async def refresh_once(self) -> bool:
        """拉目录并按差异连接/下线能力; 返回目录同步是否成功(能力级失败见 status)。"""
        async with self._refresh_lock:
            if self._closing:
                return False
            try:
                caps = await self._fetch_sync()
            except Exception as e:
                self.last_error = format_market_error("目录同步失败", e)
                logger.warning(f"平台 MCP {self.last_error}(保留既有连接)")
                return False
            self.last_error = ""
            current = {c["name"]: c for c in caps}
            for name in [n for n in list(self._caps) if n not in current]:
                await self._close_capability(name)
            items = list(current.values())
            for start in range(0, len(items), MARKET_CONNECT_BATCH_SIZE):
                batch = items[start:start + MARKET_CONNECT_BATCH_SIZE]
                results = await asyncio.gather(
                    *(self._ensure_capability(item) for item in batch),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                        logger.warning(f"平台 MCP 能力处理异常(忽略): {result}")
                if start + MARKET_CONNECT_BATCH_SIZE < len(items):
                    await self._sleep(MARKET_CONNECT_BATCH_INTERVAL)
            self._rebuild_tools()
            self.last_refresh = datetime.now().isoformat()
            self._ensure_retry_task()
            self._retry_wake.set()
            return True

    async def _fetch_sync(self) -> list[dict[str, Any]]:
        client = self._sync_client
        if client is None:
            client = httpx.AsyncClient(transport=self._transport, timeout=self.config.timeout)
            self._sync_client = client
        response = await client.get(
            f"{self.config.base_url}/api/capabilities/sync",
            headers=self._auth_headers(),
        )
        response.raise_for_status()
        return parse_sync_capabilities(response.json())

    async def _ensure_capability(self, item: dict[str, Any]) -> None:
        """确保能力已连接; 版本变化重建连接; 失败保留状态并交给快速重试队列。"""
        name = item["name"]
        version = item.get("version", "")
        cap = self._caps.get(name)
        if cap is not None and cap.version != version:
            await self._close_capability(name)
            cap = None
        if cap is None:
            cap = _CapabilityState(
                name=name, version=version,
                url=relay_stream_url(self.config.base_url, name, version=version,
                                     gateway=item.get("gateway")),
            )
            self._caps[name] = cap
        if cap.connected or (cap._conn_task is not None and not cap._conn_task.done()):
            return
        await self._connect_capability(cap)

    async def _connect_capability(self, cap: _CapabilityState) -> bool:
        """建连并登记退避: 成功清退避计数, 失败按 30s→…→刷新周期 排下次快速重试。"""
        cap._close_requested = asyncio.Event()
        cap._ready = asyncio.get_running_loop().create_future()
        cap._conn_task = asyncio.create_task(self._capability_main(cap))
        try:
            connected = bool(await cap._ready)
        except asyncio.CancelledError:
            if cap._close_requested is not None:
                cap._close_requested.set()
            raise
        if connected:
            cap.backoff = 0.0
            cap.retry_at = 0.0
        else:
            backoff = next_backoff(cap.backoff, self.config.refresh_seconds)
            cap.backoff = backoff
            cap.retry_at = self._now() + backoff if backoff > 0 else 0.0
        return connected

    def _ensure_retry_task(self) -> None:
        """确保快速重试任务在跑(单实例; 无待重试时任务挂起等唤醒)。"""
        if self._closing:
            return
        task = self._retry_task
        if task is not None and not task.done():
            return
        try:
            self._retry_task = asyncio.get_running_loop().create_task(self._retry_loop())
        except RuntimeError:
            logger.warning("平台 MCP 快速重试任务启动失败: 需在事件循环内调用")

    async def _retry_loop(self) -> None:
        """快速重试循环: 失败能力按退避到期重连(只重试失败项), 成功项不再入队。"""
        while not self._closing:
            pending = [cap for cap in self._caps.values()
                       if not cap.connected and cap.retry_at > 0]
            if not pending:
                self._retry_wake.clear()
                await self._retry_wake.wait()
                continue
            delay = min(cap.retry_at for cap in pending) - self._now()
            if delay > 0:
                await self._sleep(delay)
                if self._closing:
                    break
            now = self._now()
            due = [cap for cap in self._caps.values()
                   if not cap.connected and 0 < cap.retry_at <= now]
            changed = False
            busy = False
            for cap in due:
                if self._closing:
                    break
                if cap._conn_task is not None and not cap._conn_task.done():
                    busy = True  # 刷新正在连它: 稍后再看, 避免空转
                    continue
                await self._connect_capability(cap)
                changed = True
            if changed:
                self._rebuild_tools()
                logger.info("平台 MCP 快速重试完成: "
                            + ", ".join(f"{c.name}={'已连接' if c.connected else '仍失败'}"
                                        for c in due))
            elif busy:
                await self._sleep(MARKET_CONNECT_BATCH_INTERVAL)

    async def _capability_main(self, cap: _CapabilityState) -> None:
        """专属连接任务: 在自身任务内进出 AsyncExitStack 并停放, 直到 close 请求收尾。

        anyio cancel scope 要求进出同任务(与本地 stdio manager 同理), 故连接、
        list_tools 与收尾都在本任务内完成, 连接期间不被调用方取消打断。
        """
        ready = cap._ready
        close_requested = cap._close_requested
        try:
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(self.config.timeout):
                        session = await stack.enter_async_context(
                            self._session_opener(cap.url, self._auth_headers()))
                        mcp_tools = await session.list_tools()
                except asyncio.CancelledError:
                    if ready is not None and not ready.done():
                        ready.set_result(False)
                    raise
                except Exception as e:
                    message = format_market_error("连接失败", e)
                    logger.warning(f"平台 MCP [{cap.name}] {message}")
                    cap.set_last_error(message)
                    cap.last_refresh = datetime.now().isoformat()
                    if ready is not None and not ready.done():
                        ready.set_result(False)
                    return

                cap.session = session
                cap.tools = list(getattr(mcp_tools, "tools", None) or [])
                cap.connected = True
                cap.clear_last_error()
                cap.last_refresh = datetime.now().isoformat()
                if ready is not None and not ready.done():
                    ready.set_result(True)
                if close_requested is not None:
                    await close_requested.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            logger.debug(f"平台 MCP [{cap.name}] 连接收尾异常(可忽略): {e}")
        finally:
            cap.session = None
            cap.connected = False

    async def _close_capability(self, name: str) -> None:
        cap = self._caps.pop(name, None)
        if cap is None:
            return
        await self._cleanup_capability(cap)

    async def _cleanup_capability(self, cap: _CapabilityState) -> None:
        """安全关闭连接(独立任务 + shielded 等待, 与本地 manager 关闭语义一致)。"""
        task = cap._conn_task
        cap._conn_task = None
        if cap._close_requested is not None:
            cap._close_requested.set()
        cap.connected = False
        if task is None:
            return
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue  # 等待方被取消: 连接任务不受影响, 继续等它收尾
            except Exception:
                break
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"平台 MCP [{cap.name}] 连接收尾回调异常(可忽略): {e}")
        cap.session = None
        cap.connected = False

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.service_token}"}

    # ---------- 工具表 / 映射 / 调用(与 MCPManager 同接口) ----------

    def _rebuild_tools(self) -> None:
        """重建暴露工具表与映射(未连接能力不暴露); 变更后通知已挂接的 manager。"""
        defs: list[dict[str, Any]] = []
        to_cap: dict[str, str] = {}
        to_raw: dict[str, str] = {}
        to_risk: dict[str, str] = {}
        for cap in self._caps.values():
            if not cap.connected:
                continue
            for tool in cap.tools:
                raw_name = str(getattr(tool, "name", "") or "")
                if not raw_name:
                    continue
                exposed = exposed_tool_name(cap.name, raw_name)
                if exposed in to_raw:
                    continue
                to_cap[exposed] = cap.name
                to_raw[exposed] = raw_name
                to_risk[exposed] = classify_tool_risk(getattr(tool, "annotations", None))
                defs.append({
                    "type": "function",
                    "function": {
                        "name": exposed,
                        "description": getattr(tool, "description", "") or "",
                        "parameters": getattr(tool, "input_schema", None) or {"type": "object"},
                    },
                })
        self.tool_defs = defs
        self._exposed_to_cap = to_cap
        self._exposed_to_raw = to_raw
        self._exposed_to_risk = to_risk
        self._notify_managers()

    def attach_manager(self, manager: Any) -> None:
        """登记挂接的 MCPManager; 工具集合变化时通知其重建工具表。"""
        try:
            self._managers.add(manager)
        except TypeError:
            return

    def _notify_managers(self) -> None:
        for manager in list(self._managers):
            try:
                manager._rebuild_tool_defs()
            except Exception as e:
                logger.warning(f"平台 MCP 工具表同步到 manager 失败(忽略): {e}")

    def has_tool(self, exposed_name: str) -> bool:
        return exposed_name in self._exposed_to_raw

    def tool_risk(self, exposed_name: str) -> str | None:
        risk = self._exposed_to_risk.get(exposed_name)
        if risk is None:
            return None
        return risk if risk in MCP_RISK_LEVELS else "unknown"

    def tool_server(self, exposed_name: str) -> str | None:
        """暴露名 → 审计 server 名(``platform:{能力名}``), 非平台工具返回 None。"""
        capability = self._exposed_to_cap.get(exposed_name)
        return f"platform:{capability}" if capability else None

    def tool_raw(self, exposed_name: str) -> str | None:
        """暴露名 → 能力侧原始工具名; 非平台工具返回 None。"""
        return self._exposed_to_raw.get(exposed_name)

    async def call_tool(self, exposed_name: str, args: dict) -> str:
        """调用平台工具(入参为暴露名, 映射回能力与原始工具名)。"""
        capability = self._exposed_to_cap.get(exposed_name)
        raw_name = self._exposed_to_raw.get(exposed_name)
        if capability is None or raw_name is None:
            return f"工具 {exposed_name} 未找到"
        server = f"platform:{capability}"
        cap = self._caps.get(capability)
        if cap is None or not cap.connected or cap.session is None:
            return f"MCP [{server}] 未连接"
        async with cap.semaphore:
            try:
                result = await asyncio.wait_for(
                    cap.session.call_tool(raw_name, args), timeout=self.config.timeout)
                return format_tool_result(result)
            except asyncio.TimeoutError:
                message = f"工具调用超时: {raw_name}"
                logger.error(f"平台 MCP [{server}] {message}")
                cap.set_last_error(message)
                return "执行失败: 工具调用超时"
            except Exception as e:
                message = format_market_error(f"工具调用失败: {raw_name}", e)
                logger.error(f"平台 MCP [{server}] {message}")
                cap.set_last_error(message)
                if isinstance(e, (ConnectionError, OSError, BrokenPipeError)):
                    cap.connected = False
                return f"执行失败: {e}"

    # ---------- 状态 ----------

    def status(self) -> dict[str, Any]:
        """平台轨状态(供 /healthz 与 /api/admin/mcp 汇总; 不含令牌)。"""
        servers: list[dict[str, Any]] = []
        connected = 0
        for name in sorted(self._caps):
            cap = self._caps[name]
            servers.append({
                "name": cap.name,
                "version": cap.version,
                "connected": bool(cap.connected),
                "tools": len(cap.tools),
                "last_error": cap.last_error,
                "last_refresh": cap.last_refresh,
                "source": "platform",
            })
            if cap.connected:
                connected += 1
        return {
            "enabled": self.config.enabled,
            "base_url": self.config.base_url,
            "last_refresh": self.last_refresh,
            "last_error": self.last_error,
            "servers": servers,
            "summary": {"connected": connected, "failed": len(servers) - connected,
                        "total": len(servers)},
        }
