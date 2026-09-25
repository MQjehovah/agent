"""能力市场云端运行时工具(代授权/OBO): 按当前提问者(subject)身份调用市场能力。

与平台 MCP 轨(mcps/platform.py)的区别:
- 平台 MCP 轨是**持久会话**连接, act-as 每 worker 恒定(无法逐请求变化), 适合服务面/全局能力;
- 本工具走市场 ``/api/runtime/*`` **逐请求** HTTP, 携带 ``X-Act-As-Sub`` = 当前 run 的真实用户,
  市场侧按 actor ∩ subject 求交(见 market 的 require_runtime_access_obo), 归因与审计落到 subject。
  适合"零号员工"等全局单例为不同提问者执行**用户级**能力。

仅当市场配置齐备(MARKET_BASE_URL + MARKET_SERVICE_TOKEN)时保留, 见 Agent._init_market_runtime。
"""

import json
import logging
import os

import httpx

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

# 市场运行时端点(与 market/backend/app/routers/runtime.py 对齐)
_TOOL_INVOKE_PATH = "/api/runtime/tools/{capability}/invoke"
_AGENT_TASK_PATH = "/api/runtime/agents/{capability}/tasks"
_MCP_CALL_PATH = "/api/runtime/mcp/{capability}/call"


def _market_config() -> tuple[str, str, float]:
    """读取市场配置(base_url, service_token, timeout)。

    镜像 ``mcps.platform.PlatformMCPConfig.from_env`` 语义; 此处刻意不 import ``mcps``,
    以避免仅为读配置而引入 MCP SDK(mcps 包初始化会拉起 MCP 客户端依赖)。
    """
    base = os.environ.get("MARKET_BASE_URL", "").strip().rstrip("/")
    token = os.environ.get("MARKET_SERVICE_TOKEN", "").strip()
    try:
        timeout = float(os.environ.get("MARKET_PLATFORM_TIMEOUT", "60") or "60")
    except ValueError:
        timeout = 60.0
    if timeout <= 0:
        timeout = 60.0
    return base, token, timeout


def _resolve_subject() -> str:
    """当前 run 的提问者(subject) → 市场用户名(= rbac 工号); 解析失败返回空串。"""
    try:
        from agent.core import current_run
        raw = getattr(current_run(), "user_id", "") or ""
    except Exception:
        raw = ""
    if not raw:
        return ""
    try:
        from web.security import resolve_market_act_as
        return resolve_market_act_as(raw)
    except Exception:
        return ""


class MarketRuntimeTool(BuiltinTool):
    """按用户身份调用市场能力(工具调用 / Agent 任务)。"""

    @property
    def name(self) -> str:
        return "market_runtime"

    @property
    def description(self) -> str:
        return (
            "以当前提问者的权限(市场代授权)调用能力市场的云端能力: "
            "kind=tool 调用工具能力(params 为参数对象); kind=mcp 调用连接器能力暴露的工具"
            "(tool 指定工具名, params 为参数对象); kind=agent 向某 Agent 下发任务(task)。"
            "仅能使用提问者本人有权访问的能力, 不会越权。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "能力名称(市场中的 name), 如某工具/某 Agent",
                },
                "kind": {
                    "type": "string",
                    "enum": ["tool", "mcp", "agent"],
                    "description": "能力类型: tool=工具(默认), mcp=连接器(调用其工具), agent=下发 Agent 任务",
                },
                "tool": {
                    "type": "string",
                    "description": "kind=tool/mcp 时的工具名(该能力暴露的具体工具)",
                },
                "params": {
                    "type": "object",
                    "description": "kind=tool 时传给工具的参数对象",
                },
                "task": {
                    "type": "string",
                    "description": "kind=agent 时的任务描述",
                },
            },
            "required": ["capability"],
        }

    async def execute(self, capability: str = "", kind: str = "tool",
                      tool: str = "", params: dict | None = None,
                      task: str = "", **kwargs) -> str:
        capability = (capability or "").strip()
        kind = (kind or "tool").strip().lower()
        if not capability:
            return json.dumps({"ok": False, "error": "缺少 capability"},
                              ensure_ascii=False)

        # 用户级代授权: 无 subject 一律 fail-closed(不得以服务身份越权执行)
        subject = _resolve_subject()
        if not subject:
            return json.dumps(
                {"ok": False, "error": "无法解析当前用户的市场身份，已拒绝代授权调用"},
                ensure_ascii=False,
            )

        base, token, timeout = _market_config()
        if not (base and token):
            return json.dumps({"ok": False, "error": "能力市场未配置"},
                              ensure_ascii=False)

        if kind == "agent":
            path = _AGENT_TASK_PATH.format(capability=capability)
            body = {"task": task or ""}
        elif kind == "mcp":
            path = _MCP_CALL_PATH.format(capability=capability)
            body = {"tool": tool or "", "params": params or {}}
        else:
            path = _TOOL_INVOKE_PATH.format(capability=capability)
            body = {"tool": tool or "", "params": params or {}}

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Act-As-Sub": subject,
            "Content-Type": "application/json",
        }
        url = f"{base}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            logger.warning(f"市场运行时调用失败: {e}")
            return json.dumps({"ok": False, "error": f"市场请求失败: {e}"},
                              ensure_ascii=False)

        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": (resp.text or "")[:2000]}
        out = {"ok": 200 <= resp.status_code < 300, "status_code": resp.status_code,
               "result": payload}
        return json.dumps(out, ensure_ascii=False)
