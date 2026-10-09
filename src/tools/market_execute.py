"""能力市场执行工具(用户身份): 按当前提问者的用户 token 调用市场能力。

`kind` 支持四类能力:
- ``tool``  : 调用市场工具能力（``/api/runtime/tools/{capability}/invoke``）
- ``mcp``   : 调用连接器能力暴露的工具（``/api/runtime/mcp/{capability}/call``）
- ``skill`` : 激活技能（``/api/runtime/skills/{capability}/activate``）
- ``agent`` : 向某专家(agent)下发任务（``/api/runtime/agents/{capability}/tasks``）——
  即「capability 指向一个 agent」的委派路径（原 ``market_delegate`` 的能力已并入此处）。

与平台 MCP 轨(mcps/platform.py)的区别:
- 平台 MCP 轨是**持久会话**连接, 用户 token 每 worker 恒定(随周期刷新轮换重连), 适合服务面/全局能力;
- 本工具走市场 ``/api/runtime/*`` **逐请求** HTTP, 默认携带当前提问者的**用户 token**
  (Bearer, audience=gateway), 由市场按个人凭据执行; 适合"零号员工"等全局单例
  为不同提问者执行**用户级**能力。无托管 token 或凭据缺失时普通用户 fail-closed
  并引导登录; **管理员/系统任务**(归属 admin 账号, role=admin)由 agent 侧兜底:
  无用户令牌直接以服务令牌(平台身份)执行; 个人凭据缺失(市场 403 +
  ``X-Market-Error-Code: user_credentials_missing``)时以服务令牌重试一次。

仅当市场配置齐备(MARKET_BASE_URL + MARKET_SERVICE_TOKEN)时保留, 见 Agent._init_market_tools;
MARKET_SERVICE_TOKEN 平时仅作启用门禁, 管理员兜底路径作为平台身份令牌使用(Bearer)。
"""

import asyncio
import json
import logging

import httpx

from . import BuiltinTool
from .market_common import market_config
from .user_token import is_admin_run, resolve_user_token_or_hint

logger = logging.getLogger("agent.tools")

# 市场运行时端点(与 market/backend/app/routers/runtime.py 对齐)
_TOOL_INVOKE_PATH = "/api/runtime/tools/{capability}/invoke"
_AGENT_TASK_PATH = "/api/runtime/agents/{capability}/tasks"
_MCP_CALL_PATH = "/api/runtime/mcp/{capability}/call"
_SKILL_ACTIVATE_PATH = "/api/runtime/skills/{capability}/activate"
# 「个人凭据缺失」机器可读标记（与 market capability_secrets 常量一致，改动需双端同步）
_CREDS_MISSING_HEADER = "X-Market-Error-Code"
_CREDS_MISSING_CODE = "user_credentials_missing"


class MarketExecuteTool(BuiltinTool):
    """按用户身份执行市场能力(工具 / 连接器 / 技能 / Agent 专家)。"""

    @property
    def name(self) -> str:
        return "market_execute"

    @property
    def description(self) -> str:
        return (
            "逐请求以当前提问者的用户 token 执行能力市场的云端能力: "
            "kind=tool 调用工具能力(params 为参数对象); kind=mcp 调用连接器能力暴露的工具"
            "(tool 指定工具名, params 为参数对象); kind=skill 激活技能(返回技能说明文本, task 为使用场景); "
            "kind=agent 把任务下发给市场专家(agent): capability=专家名, task=任务描述(即委派专家)。"
            "仅能使用提问者本人有权访问的能力, 不会越权。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "能力名称(市场中的 name): 工具/连接器/技能/专家(agent)名",
                },
                "kind": {
                    "type": "string",
                    "enum": ["tool", "mcp", "skill", "agent"],
                    "description": "能力类型: tool=工具(默认), mcp=连接器, skill=技能, agent=委派专家",
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
                    "description": "kind=agent 时交给专家的任务描述; kind=skill 时的使用场景(可选)",
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
            return json.dumps({"success": False, "ok": False, "error": "缺少 capability"},
                              ensure_ascii=False)

        base, token, timeout = market_config()
        if not (base and token):
            return json.dumps({"success": False, "ok": False, "error": "能力市场未配置"},
                              ensure_ascii=False)

        # 身份与凭据（agent 侧决策，市场零角色判断）:
        # - 默认逐请求携带提问者用户令牌 → 市场按个人凭据执行;
        # - 个人凭据缺失时市场返回 403 + X-Market-Error-Code=user_credentials_missing;
        #   管理员/系统身份(role=admin) 收到该标记后改用服务令牌(平台身份)重试一次，
        #   由市场按平台凭据执行;
        # - 管理员本就没有市场用户令牌时，直接以服务令牌（平台身份）执行;
        # - 普通用户无令牌/凭据缺失一律 fail-closed（不得以服务身份越权执行）。
        # token 解析含同步阻塞(SSO 刷新/交换 + SQLite), 经线程池执行避免卡事件循环
        is_admin = is_admin_run()
        user_token, hint = await asyncio.to_thread(resolve_user_token_or_hint)
        if hint and not is_admin:
            return json.dumps({"success": False, "ok": False, "error": hint}, ensure_ascii=False)
        using_platform = bool(hint)  # 管理员无用户令牌 → 直接平台身份
        if using_platform:
            user_token = token

        if kind == "agent":
            path = _AGENT_TASK_PATH.format(capability=capability)
            body = {"task": task or ""}
        elif kind == "mcp":
            path = _MCP_CALL_PATH.format(capability=capability)
            body = {"tool": tool or "", "params": params or {}}
        elif kind == "skill":
            path = _SKILL_ACTIVATE_PATH.format(capability=capability)
            body = {"context": task or ""}
        else:
            path = _TOOL_INVOKE_PATH.format(capability=capability)
            body = {"tool": tool or "", "params": params or {}}

        url = f"{base}{path}"
        # Agent 任务在云端跑完整推理链，通常远超默认 60s；单独放宽避免误判为"空错误"超时
        call_timeout = max(timeout, 300.0) if kind == "agent" else timeout
        try:
            async with httpx.AsyncClient(timeout=call_timeout) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {user_token}",
                             "Content-Type": "application/json"},
                    json=body,
                )
                # 管理员 + 个人凭据缺失 → 平台身份重试一次（403 发生于执行前，重试安全）
                if (
                    not using_platform
                    and is_admin
                    and resp.status_code == 403
                    and resp.headers.get(_CREDS_MISSING_HEADER) == _CREDS_MISSING_CODE
                ):
                    logger.info(f"管理员 {capability} 个人凭据缺失, 以平台身份重试")
                    resp = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {token}",
                                 "Content-Type": "application/json"},
                        json=body,
                    )
        except httpx.HTTPError as e:
            logger.warning(f"市场执行调用失败: {e}")
            return json.dumps({"success": False, "ok": False, "error": f"市场请求失败: {type(e).__name__}: {e}"},
                              ensure_ascii=False)

        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": (resp.text or "")[:2000]}
        # 市场 runtime 统一信封 {ok, action, capability, message, result}: 取内层 result，
        # 使 tool/mcp/skill/agent 四类对模型呈现一致；无 result 键则原样返回(兼容)。
        inner = payload.get("result") if isinstance(payload, dict) else None
        if inner is None:
            inner = payload
        ok = 200 <= resp.status_code < 300
        out = {"success": ok, "ok": ok, "status_code": resp.status_code,
               "result": inner}
        return json.dumps(out, ensure_ascii=False)
