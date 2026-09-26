"""能力市场目录检索工具(代授权/OBO): 零号员工"全能"的发现层。

按要办的事(task/关键词)在市场检索可用能力(agent/skill/mcp/tool/workflow),
逐请求携带 ``X-Act-As-Sub`` = 当前提问者, 由市场按 **subject 视角**过滤可见性;
命中后可用 ``market_runtime`` 执行。
"""

import json
import logging

import httpx

from . import BuiltinTool
from .market_common import market_config, resolve_subject

logger = logging.getLogger("agent.tools")

# 市场目录检索端点(portal /api/capabilities/task-search, 支持 X-Act-As-Sub)
_TASK_SEARCH_PATH = "/api/capabilities/task-search"


class MarketSearchTool(BuiltinTool):
    """在市场按要办的事检索能力(专家/技能/连接器/工具/编排)。"""

    @property
    def name(self) -> str:
        return "market_search"

    @property
    def description(self) -> str:
        return (
            "在能力市场按要办的事检索可用能力(以当前提问者视角): 返回 agent(专家)/skill(技能)/"
            "mcp(连接器)/tool(工具) 清单及简介。用于**先发现再调用**——找到合适的后，"
            "用 market_runtime 执行(kind=tool/mcp/skill/agent)。"
            "可用 `kind` 只看某一类(如先 kind=\"agent\" 找专家；没有再 kind=\"skill\"/\"mcp\"/\"tool\")。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要办的事 / 关键词(自然语言)"},
                "kind": {
                    "type": "string",
                    "enum": ["agent", "skill", "mcp", "tool", "all"],
                    "description": "只看某类能力: agent=专家, skill=技能, mcp=连接器, tool=工具; 默认 all(全部)",
                },
                "limit": {"type": "integer", "description": "每组返回上限, 默认 8"},
            },
            "required": ["query"],
        }

    async def execute(self, query: str = "", kind: str = "all", limit: int = 8, **kwargs) -> str:
        q = (query or "").strip()
        if not q:
            return json.dumps({"ok": False, "error": "缺少 query"}, ensure_ascii=False)
        k = (kind or "all").strip().lower()
        if k == "expert":
            k = "agent"
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 20))

        base, token, timeout = market_config()
        if not (base and token):
            return json.dumps({"ok": False, "error": "能力市场未配置"}, ensure_ascii=False)

        # 逐请求代授权: 无 subject 时以服务令牌检索会把"全量"能力暴露给提问者, 故 fail-closed
        subject = resolve_subject()
        if not subject:
            return json.dumps(
                {"ok": False, "error": "无法解析当前用户的市场身份，已拒绝检索"},
                ensure_ascii=False,
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Act-As-Sub": subject,
        }
        url = f"{base}{_TASK_SEARCH_PATH}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers, params={"q": q})
        except httpx.HTTPError as e:
            logger.warning(f"市场检索失败: {e}")
            return json.dumps({"ok": False, "error": f"市场请求失败: {e}"}, ensure_ascii=False)

        if resp.status_code >= 400:
            return json.dumps(
                {"ok": False, "status_code": resp.status_code, "error": (resp.text or "")[:500]},
                ensure_ascii=False,
            )
        try:
            data = resp.json()
        except Exception:
            return json.dumps({"ok": False, "error": "市场返回非 JSON"}, ensure_ascii=False)

        def _row(hit: dict) -> dict:
            return {
                "name": hit.get("name", ""),
                "display_name": hit.get("display_name") or "",
                "type": hit.get("type", ""),
                "description": (hit.get("description") or "")[:200],
                "score": hit.get("match_score"),
            }

        groups = {
            "agent": data.get("agents", []),
            "skill": data.get("skills", []),
            "mcp": data.get("mcps", []),
            "tool": list(data.get("others", [])) + list(data.get("plugins", [])),
        }
        plural = {"agent": "agents", "skill": "skills", "mcp": "mcps", "tool": "tools"}
        out: dict = {"ok": True, "query": data.get("q", q), "terms": data.get("terms", [])}
        total = 0
        if k in plural:
            items = [_row(r) for r in groups[k][:limit]]
            out[plural[k]] = items
            out["kind"] = k
            total = len(items)
        else:
            for key, rows in groups.items():
                items = [_row(r) for r in rows[:limit]]
                out[plural[key]] = items
                total += len(items)
        out["total"] = total
        if total == 0:
            out["hint"] = "未命中，可换更贴近的词语再检索" if k not in plural else "该类型下未命中，可换词或 kind=all 看全部"
        return json.dumps(out, ensure_ascii=False)
