"""
AgentFactory — 唯一的 Agent 创建入口

系统中只有一种 Agent 类，区别仅在于 config_dir 指向不同的 PROMPT.md。
个人子代理、团队成员都是 Agent 实例。
"""
import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from utils.frontmatter import extract_frontmatter

if TYPE_CHECKING:
    from agent.core import Agent

logger = logging.getLogger("agent.factory")


@dataclass
class AgentInstance:
    """Agent 实例包装（替代 SubagentInstance）"""
    agent: "Agent"
    template: str
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    task_count: int = 0


class AgentFactory:
    """Agent 工厂 — 从 template 创建 Agent 实例"""

    CLEANUP_INTERVAL = 300
    AGENT_TTL = 3600

    def __init__(self, config_dir: str, base_workspace: str = ""):
        self.config_dir = config_dir
        self.base_workspace = base_workspace or config_dir
        self.templates: dict[str, dict] = {}
        self._active_agents: dict[str, AgentInstance] = {}
        self._name_to_session: dict[str, str] = {}
        self._team_configs: dict[str, dict] = {}
        self._team_members: dict[str, dict[str, dict]] = {}
        self._team_member_cache: dict[str, dict] = {}
        self._client = None
        self._parent_agent = None
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self.scan()

    # ── 模板扫描 ──────────────────────────────────

    def scan(self):
        """扫描 config/agents/ 加载所有模板（个人 + 团队）"""
        agents_dir = os.path.join(self.config_dir, "agents")
        if not os.path.isdir(agents_dir):
            return
        for dir_name in os.listdir(agents_dir):
            agent_dir = os.path.join(agents_dir, dir_name)
            if not os.path.isdir(agent_dir):
                continue
            team_file = os.path.join(agent_dir, "TEAM.md")
            agts_dir = os.path.join(agent_dir, "agents")
            if os.path.exists(team_file) and os.path.isdir(agts_dir):
                self._load_team(dir_name, agent_dir, team_file, agts_dir)
                continue
            prompt_file = os.path.join(agent_dir, "PROMPT.md")
            if os.path.exists(prompt_file):
                with open(prompt_file, encoding="utf-8") as f:
                    content = f.read()
                fm, _ = extract_frontmatter(content)
                if fm:
                    name = fm.get("name", dir_name)
                    self.templates[name] = {
                        "name": name, "description": fm.get("description", ""),
                        "workspace": self.base_workspace, "config_dir": agent_dir,
                    }

    def scan_teams(self) -> list[str]:
        """返回所有可用的 template 名称列表"""
        return [k for k in self.templates]

    # ── 团队加载 ──────────────────────────────────

    def _load_team(self, dir_name: str, agent_dir: str, team_file: str, agents_dir: str):
        with open(team_file, encoding="utf-8") as f:
            content = f.read()
        fm, body = extract_frontmatter(content)
        if not fm:
            return
        name = fm.get("name", dir_name)
        raw_members = fm.get("members", [])
        team_roles = ""
        if isinstance(raw_members, list):
            lines = []
            for m in raw_members:
                if isinstance(m, dict):
                    lines.append(f"- {m.get('name', '')}: {m.get('role', '')}")
                else:
                    lines.append(f"- {m}")
            team_roles = "\n".join(lines)
        pf = os.path.join(agent_dir, "PROMPT.md")
        prompt_body = ""
        if os.path.exists(pf):
            with open(pf, encoding="utf-8") as f:
                _, prompt_body = extract_frontmatter(f.read())
        config = {
            "name": name, "description": fm.get("description", ""),
            "leader": fm.get("leader", ""), "pipeline_mode": fm.get("pipeline_mode", "feedback"),
            "workspace": self.base_workspace, "team_body": body, "team_roles": team_roles,
            "leader_prompt": prompt_body, "dir_name": dir_name,
            "tool_denylist": set(fm.get("tool_denylist", [
                "subagent", "knowledge_search", "web_search", "web_fetch",
                "task_create", "task_list", "task_get", "task_cancel",
                "ask_user", "memory",
            ])),
        }
        self._team_configs[name] = config
        members = {}
        for mn in os.listdir(agents_dir):
            mp = os.path.join(agents_dir, mn)
            if os.path.isdir(mp):
                template = self._get_member_template(dir_name, mn)
                if template:
                    members[mn] = template
        self._team_members[name] = members
        self.templates[name] = {"name": name, "description": config["description"],
                                 "workspace": self.base_workspace, "is_team": True}
        logger.info(f"加载团队: {name}, 成员: {list(members.keys())}")

    def _get_member_template(self, team_name: str, member_name: str) -> dict | None:
        cache_key = f"{team_name}/{member_name}"
        if cache_key in self._team_member_cache:
            return self._team_member_cache[cache_key]
        member_dir = os.path.join(self.config_dir, "agents", team_name, "agents", member_name)
        prompt_file = os.path.join(member_dir, "PROMPT.md")
        if not os.path.exists(prompt_file):
            return None
        with open(prompt_file, encoding="utf-8") as f:
            content = f.read()
        fm, _ = extract_frontmatter(content)
        if not fm:
            return None
        template = {"name": fm.get("name", member_name), "description": fm.get("description", ""),
                     "workspace": self.base_workspace, "config_dir": member_dir}
        self._team_member_cache[cache_key] = template
        return template

    # ── 团队查询 ──────────────────────────────────

    def is_team(self, name: str) -> bool:
        return name in self._team_configs

    def get_team_config(self, name: str) -> dict | None:
        return self._team_configs.get(name)

    def get_team_members(self, name: str) -> dict | None:
        return self._team_members.get(name)

    # ── 创建 Agent ────────────────────────────────

    async def create(self, template: str = "", config_dir: str = "",
                     name: str = "", client=None, parent_agent=None,
                     session_id: str = "", system_prompt: str = "",
                     tools: list[str] = None, mcp_servers: list = None,
                     max_iterations: int = 0) -> tuple["Agent", str]:
        """根据 template 创建或复用 Agent 实例"""
        from agent.core import Agent

        template_data = self.templates.get(template) if template else None
        if config_dir:
            agent_dir = config_dir
        elif template_data:
            agent_dir = template_data.get("config_dir", "")
        else:
            agent_dir = ""
        workspace = (template_data.get("workspace") if template_data else None) or self.base_workspace

        # 检查是否已存在
        lookup_key = name or template
        if lookup_key and lookup_key in self._name_to_session:
            sid = self._name_to_session[lookup_key]
            if sid in self._active_agents:
                inst = self._active_agents[sid]
                inst.last_used = time.time()
                inst.task_count += 1
                return inst.agent, sid

        agent = Agent(workspace=workspace, client=client or self._client,
                      parent_agent=parent_agent or self._parent_agent,
                      config_dir=agent_dir, mcp_servers=mcp_servers or [])
        if parent_agent or self._parent_agent:
            agent.plugin_manager = (parent_agent or self._parent_agent).plugin_manager

        if system_prompt:
            agent.system_prompt = system_prompt

        await agent.initialize()
        if max_iterations > 0:
            agent.max_iterations = max_iterations

        new_sid = session_id or str(uuid.uuid4())[:8]
        inst = AgentInstance(agent=agent, template=template or lookup_key, session_id=new_sid)
        self._active_agents[new_sid] = inst
        if lookup_key:
            self._name_to_session[lookup_key] = new_sid

        self._forward_hooks(agent, template)
        return agent, new_sid

    async def create_team_member(self, team_name: str, role: str,
                                  client=None, parent_agent=None,
                                  max_iterations: int = 0) -> "Agent":
        """创建团队成员 Agent"""
        from agent.core import Agent
        template = self._get_member_template(team_name, role)
        if not template:
            raise ValueError(f"团队 {team_name} 中未找到成员 {role}")
        workspace = template.get("workspace") or self.base_workspace
        config_dir = template.get("config_dir", "")
        agent = Agent(workspace=workspace, client=client or self._client,
                      parent_agent=parent_agent or self._parent_agent,
                      config_dir=config_dir)
        if parent_agent or self._parent_agent:
            agent.plugin_manager = (parent_agent or self._parent_agent).plugin_manager
        await agent.initialize()
        if max_iterations > 0:
            agent.max_iterations = max_iterations
        team_config = self._team_configs.get(team_name, {})
        agent.tool_denylist = team_config.get("tool_denylist", {
            "subagent", "knowledge_search", "web_search", "web_fetch",
            "task_create", "task_list", "task_get", "task_cancel",
            "ask_user", "memory",
        })
        skills_dir = os.path.join(self.config_dir, "agents", team_name, "skills")
        if os.path.exists(skills_dir):
            from skills import SkillManager
            if not agent.skill_manager:
                agent.skill_manager = SkillManager(skills_dir)
            else:
                tsm = SkillManager(skills_dir)
                for sn in tsm.list_skills():
                    if sn not in agent.skill_manager.skills:
                        sk = tsm.get_skill(sn)
                        if sk:
                            agent.skill_manager.skills[sn] = sk
            agent.skill_manager._build_builtin_tools()
        if agent.skill_manager:
            skill_names = agent.skill_manager.list_skills()
            if skill_names:
                guide = (f"\n\n## 技能工具\n你有一个 skill 工具，可加载结构化工作流指引。\n"
                         f"可用技能: {', '.join(skill_names)}")
                agent.system_prompt += guide
                agent.system_prompt_raw += guide
        self._forward_hooks(agent, f"{team_name}/{role}")
        return agent

    # ── Hook 转发 ─────────────────────────────────

    def _forward_hooks(self, child_agent, template_name: str):
        from hooks import HookEvent
        parent = self._parent_agent
        if not parent:
            return
        name = template_name or child_agent.name or "unknown"

        async def on_chat(ctx):
            await parent.hooks.fire(HookEvent.SUBAGENT_CHAT_EVENT, token=ctx.token, agent_name=name)
        async def on_tool_start(ctx):
            await parent.hooks.fire(HookEvent.SUBAGENT_TOOL_START, tool_name=ctx.tool_name,
                                     arguments=ctx.arguments, agent_name=name)
        async def on_tool_result(ctx):
            await parent.hooks.fire(HookEvent.SUBAGENT_TOOL_RESULT, tool_name=ctx.tool_name,
                                     result=ctx.result, agent_name=name)
        async def on_round_start(ctx):
            await parent.hooks.fire(HookEvent.SUBAGENT_ROUND_START, agent_name=name, metadata=ctx.metadata)

        child_agent.hooks.register(HookEvent.CHAT_EVENT, on_chat)
        child_agent.hooks.register(HookEvent.TOOL_START, on_tool_start)
        child_agent.hooks.register(HookEvent.TOOL_RESULT, on_tool_result)
        child_agent.hooks.register(HookEvent.ROUND_START, on_round_start)

    # ── 提示词 ────────────────────────────────────

    def get_subagent_prompt(self) -> str:
        if not self.templates:
            return "没有可用的子代理"
        lines = ["\n## SubAgent列表（仅限以下名称，严禁编造）\n"]
        lines.append("| 名称 | 类型 | 描述 |")
        lines.append("|------|------|------|")
        for key, td in sorted(self.templates.items()):
            if "/" in key:
                continue
            tag = "团队" if td.get("is_team") else "个人"
            desc = td.get("description", "")[:40]
            lines.append(f"| {td['name']} | {tag} | {desc} |")
        lines.append("\n**调用方式**: subagent(template=\"名称\", task=\"...\")")
        lines.append("**所有可用子代理已完整列出在上表中，严禁编造名称**\n")
        return "\n".join(lines)

    # ── 生命周期 ──────────────────────────────────

    async def cleanup_agent(self, session_id: str):
        async with self._lock:
            inst = self._active_agents.pop(session_id, None)
            if inst:
                for key, sid in list(self._name_to_session.items()):
                    if sid == session_id:
                        del self._name_to_session[key]
                await inst.agent.cleanup()

    async def cleanup_all(self):
        async with self._lock:
            agents = list(self._active_agents.values())
            self._active_agents.clear()
            self._name_to_session.clear()
        for inst in agents:
            await inst.agent.cleanup()

    def get_stats(self) -> dict:
        return {
            "active_subagents": [
                {"session_id": sid, "template": inst.template,
                 "task_count": inst.task_count}
                for sid, inst in self._active_agents.items()
            ],
            "templates": list(self.templates.keys()),
            "team_count": len(self._team_configs),
        }

    def get_sessions_by_template(self, template: str) -> list[dict]:
        return [{"session_id": sid, "task_count": inst.task_count,
                  "agent_id": inst.agent.agent_id}
                for sid, inst in self._active_agents.items()
                if inst.template == template]

    def get_all_sessions(self) -> dict[str, list[dict]]:
        grouped: dict[str, list] = {}
        for sid, inst in self._active_agents.items():
            t = inst.template or "unknown"
            grouped.setdefault(t, []).append(
                {"session_id": sid, "task_count": inst.task_count,
                 "agent_id": inst.agent.agent_id})
        return grouped

    # ── 清理任务 ──────────────────────────────────

    def start_cleanup_task(self):
        if self._cleanup_task:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.debug("Agent 清理任务已启动")

    def stop_cleanup_task(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
            logger.debug("Agent 清理任务已停止")

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(self.CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                return
            try:
                await self._cleanup_expired()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Agent 清理失败: {e}")

    async def _cleanup_expired(self):
        now = time.time()
        async with self._lock:
            expired = [sid for sid, inst in self._active_agents.items()
                       if now - inst.last_used > self.AGENT_TTL]
            for sid in expired:
                inst = self._active_agents.pop(sid, None)
                if inst:
                    for key, sid2 in list(self._name_to_session.items()):
                        if sid2 == sid:
                            del self._name_to_session[key]
        if expired:
            logger.info(f"清理 {len(expired)} 个过期 Agent")
