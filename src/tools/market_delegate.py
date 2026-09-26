"""专家委派工具(轻量无状态/OBO): 零号员工按需委派市场专家, 由 agent 引擎本地执行。

流程:
  1. 取专家人设: GET /api/runtime/agents/{expert}/persona (Bearer + X-Act-As-Sub) → PROMPT.md + 依赖清单;
  2. 校验依赖: 含 mcp 且 distribution=local 的 → 拒绝(平台不支持 stdio 本地依赖);
  3. 技能: 逐个 POST /api/runtime/skills/{name}/activate 取 SKILL.md 文本(内存缓存) → 注入人设;
  4. 组 system_prompt = 人设 + 技能文本 + 依赖(连接器/工具经 market_runtime 平台执行)说明;
  5. 起**临时子代理**(agent 引擎, 不加载本地 skill/mcp, 零文件落地)运行任务;
  6. 返回结构化结果 {ok, expert_output, error} 供零号员工判断/兜底。

仅当市场配置齐备时可用; 无 subject 一律 fail-closed。
"""

import json
import logging
import tempfile
import time

import httpx

from . import BuiltinTool
from .market_common import market_config, resolve_subject

logger = logging.getLogger("agent.tools")

_PERSONA_PATH = "/api/runtime/agents/{expert}/persona"
_SKILL_ACTIVATE_PATH = "/api/runtime/skills/{skill}/activate"

# 技能文本缓存: name -> (text, expires_at)
_SKILL_CACHE: dict[str, tuple[str, float]] = {}
_SKILL_TTL = 300.0


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


async def _fetch_skill_text(base: str, headers: dict, name: str, timeout: float) -> str:
    """取技能文本(SKILL.md); 命中内存缓存则直接返回。失败返回空串。"""
    key = (name or "").strip()
    if not key:
        return ""
    cached = _SKILL_CACHE.get(key)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}{_SKILL_ACTIVATE_PATH.format(skill=key)}", headers=headers, json={"context": ""}
            )
        if resp.status_code >= 400:
            return ""
        data = resp.json()
        text = ""
        result = data.get("result") if isinstance(data, dict) else None
        if isinstance(result, dict):
            text = str(result.get("skill_md") or "")
        if not text and isinstance(data, dict):
            text = str(data.get("skill_md") or "")
        if text:
            _SKILL_CACHE[key] = (text, now + _SKILL_TTL)
        return text
    except (httpx.HTTPError, ValueError):
        return ""


def _compose_prompt(persona: str, deps: list[dict], skill_texts: list[tuple[str, str]]) -> str:
    parts = [persona.strip()]
    if skill_texts:
        blocks = [f"## {n}\n{t}" for n, t in skill_texts if t]
        if blocks:
            parts.append("# 可用技能(按以下说明执行)\n\n" + "\n\n".join(blocks))
    mcps = [str(d.get("name")) for d in deps if str(d.get("type")) == "mcp"]
    tools = [str(d.get("name")) for d in deps if str(d.get("type")) == "tool"]
    lines = ["# 你的依赖（由能力市场平台执行）", "使用 market_runtime 工具调用下列依赖，不要臆造工具："]
    if mcps:
        lines.append(
            "- 连接器(mcp)：market_runtime(kind='mcp', capability='<名称>', tool='<工具>', params={...})。可用：" + "、".join(mcps)
        )
    if tools:
        lines.append(
            "- 工具(tool)：market_runtime(kind='tool', capability='<名称>', tool='<工具>', params={...})。可用：" + "、".join(tools)
        )
    if mcps or tools:
        parts.append("\n".join(lines))
    return "\n\n".join(p for p in parts if p)


async def _run_transient(system_prompt: str, task: str) -> dict:
    """以 agent 引擎起临时子代理执行(不加载本地 skill/mcp, 零文件落地)。"""
    from agent.core import Agent, current_agent, current_run

    parent = current_agent()
    if parent is None:
        return {"ok": False, "error": "无法获取当前 agent 上下文"}
    rc = current_run()
    tmpdir = tempfile.mkdtemp(prefix="expert-")
    sub = Agent(
        workspace=getattr(parent, "workspace", "") or "",
        client=getattr(parent, "client", None),
        parent_agent=parent,
        config_dir=tmpdir,  # 空目录: 不加载本地 PROMPT/skills/mcp
    )
    sub.persist_session = False
    sub.platform_mcp_enabled = False
    try:
        await sub.initialize()
        sub.system_prompt = system_prompt
        sub.system_prompt_raw = system_prompt
        r = await sub.run(
            task,
            session_id="",
            user_id=rc.user_id,
            user_name=rc.user_name,
            role=rc.role,
            user_department=getattr(rc, "user_department", ""),
            user_role=getattr(rc, "user_role", ""),
        )
        output = getattr(r, "result", None)
        if output is None:
            output = str(r)
        err = getattr(r, "error", "") or ""
        return {"ok": not err, "expert_output": output, "error": err}
    except Exception as exc:  # noqa: BLE001
        logger.warning("专家委派执行失败: %s", exc)
        return {"ok": False, "error": f"专家执行失败: {exc}"}
    finally:
        try:
            mcp = getattr(sub, "mcp", None)
            close = getattr(mcp, "close", None)
            if close is not None:
                await close()
        except Exception:
            pass


class MarketDelegateTool(BuiltinTool):
    """把任务委派给能力市场里的专家(agent)，由本机 agent 引擎以该专家身份执行。"""

    @property
    def name(self) -> str:
        return "market_delegate"

    @property
    def description(self) -> str:
        return (
            "把任务委派给能力市场里的**专家(agent)**: 取专家人设与依赖, 由本机 agent 引擎以该专家身份执行, "
            "返回结果。适合'找专家办事'——先用 market_search 发现专家, 再用本工具委派。"
            "专家的连接器/工具依赖会经能力市场平台执行, 不越权(按当前提问者权限)。"
        )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "expert": {"type": "string", "description": "专家名称(市场 agent 能力的 name)"},
                "task": {"type": "string", "description": "交给专家的任务描述"},
            },
            "required": ["expert", "task"],
        }

    async def execute(self, expert: str = "", task: str = "", **kwargs) -> str:
        expert = (expert or "").strip()
        task = (task or "").strip()
        if not expert:
            return _err("缺少 expert")
        if not task:
            return _err("缺少 task")

        subject = resolve_subject()
        if not subject:
            return _err("无法解析当前用户的市场身份，已拒绝委派")
        base, token, timeout = market_config()
        if not (base and token):
            return _err("能力市场未配置")

        headers = {
            "Authorization": f"Bearer {token}",
            "X-Act-As-Sub": subject,
            "Content-Type": "application/json",
        }
        # 1) 取专家人设 + 依赖
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{base}{_PERSONA_PATH.format(expert=expert)}", headers=headers)
        except httpx.HTTPError as exc:
            return _err(f"市场请求失败: {exc}")
        if resp.status_code >= 400:
            return _err(f"取专家定义失败(HTTP {resp.status_code}): {(resp.text or '')[:300]}")
        try:
            data = resp.json()
        except ValueError:
            return _err("专家定义返回非 JSON")
        persona = str((data or {}).get("prompt") or "").strip()
        deps = list((data or {}).get("dependencies") or [])
        if not persona:
            return _err("专家无人设(PROMPT.md)")

        # 2) 校验依赖: 平台不支持 local(stdio) mcp 依赖
        bad = [
            str(d.get("name"))
            for d in deps
            if str(d.get("type")) == "mcp"
            and str(d.get("distribution") or "").strip().lower() == "local"
        ]
        if bad:
            return _err(f"专家依赖含本地(stdio)连接器，平台不支持委派：{'、'.join(bad)}")

        # 3) 技能文本注入(缓存)
        skill_texts: list[tuple[str, str]] = []
        for d in deps:
            if str(d.get("type")) == "skill":
                name = str(d.get("name") or "")
                text = await _fetch_skill_text(base, headers, name, timeout)
                if text:
                    skill_texts.append((name, text))

        # 4) 组人设
        system_prompt = _compose_prompt(persona, deps, skill_texts)

        # 5) agent 引擎执行
        result = await _run_transient(system_prompt, task)
        result["expert"] = expert
        return json.dumps(result, ensure_ascii=False)
