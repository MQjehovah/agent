"""把市场里当前用户已加入的用户级 MCP 工具挂到 agent。

这是通用 MCP Host 行为：先 tools/list，再把工具注册成代理；调用时 tools/call。
不包含任何具体能力名（DWS 只是其中一个 MCP）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from . import BuiltinTool
from .market_common import market_config
from .user_token import resolve_user_token_or_hint

logger = logging.getLogger("agent.tools.market_mcp")

_PREFIX = "market__"
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def _tool_name(capability: str, tool: str) -> str:
    cap = _SAFE.sub("_", capability).strip("_")
    name = _SAFE.sub("_", tool).strip("_")
    return f"{_PREFIX}{cap}__{name}"


class MarketMCPProxyTool(BuiltinTool):
    """市场 MCP 工具的本地代理；每次调用走 /api/runtime/mcp/{cap}/call。"""

    def __init__(self, capability: str, remote_name: str, description: str, parameters: dict):
        self.capability = capability
        self.remote_name = remote_name
        self._name = _tool_name(capability, remote_name)
        self._description = description or f"市场 MCP 工具 {capability}/{remote_name}"
        self._parameters = parameters if isinstance(parameters, dict) else {
            "type": "object",
            "properties": {},
        }

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict:
        return self._parameters

    async def execute(self, **kwargs) -> str:
        base, _service, timeout = market_config()
        if not base:
            return json.dumps({"success": False, "error": "能力市场未配置"}, ensure_ascii=False)
        token, hint = await asyncio.to_thread(resolve_user_token_or_hint, audience="market")
        if hint:
            return json.dumps({"success": False, "error": hint}, ensure_ascii=False)
        url = f"{base.rstrip('/')}/api/runtime/mcp/{self.capability}/call"
        try:
            async with httpx.AsyncClient(timeout=max(timeout, 120)) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"tool": self.remote_name, "params": kwargs},
                )
        except httpx.HTTPError as exc:
            return json.dumps(
                {"success": False, "error": f"市场 MCP 请求失败: {type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        try:
            payload = resp.json()
        except Exception:  # noqa: BLE001
            return json.dumps({"success": False, "error": (resp.text or "")[:1000]}, ensure_ascii=False)
        if resp.status_code >= 400:
            return json.dumps(
                {"success": False, "status_code": resp.status_code, "error": payload},
                ensure_ascii=False,
            )
        inner = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(inner, dict) and "result" in inner:
            inner = inner.get("result")
        return json.dumps({"success": True, "capability": self.capability, "result": inner}, ensure_ascii=False)


async def mount_user_mcp_tools(agent) -> int:
    """为当前 worker 挂载已加入的用户级 MCP 工具；失败只告警。"""
    if not getattr(agent, "owner_uid", 0):
        return 0
    base, _service, timeout = market_config()
    if not base:
        return 0
    token = ""
    provider = getattr(agent, "platform_token_provider", None)
    if callable(provider):
        try:
            token = str(provider() or "")
        except Exception as exc:  # noqa: BLE001
            logger.info("市场 MCP 工具挂载跳过：用户 token 获取失败 %s", exc)
            return 0
    if not token:
        token, hint = await asyncio.to_thread(resolve_user_token_or_hint, audience="market")
        if hint:
            logger.info("市场 MCP 工具挂载跳过：%s", hint)
            return 0
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=max(timeout, 60)) as client:
            resp = await client.get(
                f"{base.rstrip('/')}/api/my/capabilities",
                params={"scope": "added"},
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning("市场 MCP 工具挂载失败：能力列表 %s", resp.status_code)
                return 0
            items = resp.json() or []
    except httpx.HTTPError as exc:
        logger.warning("市场 MCP 工具挂载失败：%s", exc)
        return 0

    mounted = 0
    for cap in items:
        if not isinstance(cap, dict):
            continue
        if str(cap.get("type") or "") != "mcp":
            continue
        if str(cap.get("binding") or "") not in ("user", "user_only"):
            continue
        name = str(cap.get("name") or "").strip()
        if not name:
            continue
        try:
            async with httpx.AsyncClient(timeout=max(timeout, 120)) as client:
                resp = await client.post(
                    f"{base.rstrip('/')}/api/runtime/mcp/{name}/connect",
                    headers=headers,
                    json={},
                )
            if resp.status_code != 200:
                logger.warning("市场 MCP [%s] tools/list 失败：%s", name, resp.status_code)
                continue
            payload = resp.json() or {}
            inner = payload.get("result") if isinstance(payload, dict) else None
            tools = (inner or {}).get("tools") if isinstance(inner, dict) else None
            if not isinstance(tools, list):
                continue
            for tool in tools:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                proxy = MarketMCPProxyTool(
                    name,
                    str(tool["name"]),
                    str(tool.get("description") or ""),
                    tool.get("inputSchema") or {},
                )
                if agent.tool_registry.has_tool(proxy.name):
                    continue
                agent.tool_registry.register_tool(proxy)
                mounted += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("市场 MCP [%s] 挂载失败：%s", name, exc)
    if mounted:
        logger.info("市场 MCP 工具已挂载 %d 个（用户 %s）", mounted, getattr(agent, "owner_tag", ""))
    return mounted
