"""
子代理管理器模块

支持子代理持久化，保持上下文连续性
"""
import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from utils.frontmatter import extract_frontmatter

if TYPE_CHECKING:
    from agent import Agent, AgentResult

logger = logging.getLogger("agent.subagent")


@dataclass
class SubagentInstance:
    """子代理实例"""
    agent: "Agent"
    template: str
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_used: float = field(default_factory=time.time)
    task_count: int = 0


class SubagentManager:
    """子代理管理器 - 支持持久化和会话复用，统一管理个人子代理和团队"""

    CLEANUP_INTERVAL = 300  # 清理间隔: 5分钟
    SUBAGENT_TTL = 3600  # 子代理存活时间: 1小时

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.templates: dict[str, dict[str, Any]] = {}
        self._active_subagents: dict[str, SubagentInstance] = {}  # session_id -> SubagentInstance
        self._name_to_session: dict[str, str] = {}  # template/name -> session_id
        self._team_member_cache: dict[str, dict[str, Any]] = {}
        self._team_configs: dict[str, dict[str, Any]] = {}
        self._team_members: dict[str, dict[str, dict[str, Any]]] = {}
        self._client = None
        self._parent_agent = None
        self._lock = asyncio.Lock()
        self._cleanup_task = None
        # Parent event callbacks for UI streaming
        self._parent_on_tool_event = None
        self._parent_on_token = None
        self._load_all()

    def _load_all(self):
        """加载所有子代理模板（包括个人和团队）"""
        if not self.base_dir or not os.path.exists(self.base_dir):
            logger.warning(f"Subagent directory not found: {self.base_dir}")
            return

        for dir_name in os.listdir(self.base_dir):
            agent_dir = os.path.join(self.base_dir, dir_name)
            if not os.path.isdir(agent_dir):
                continue

            team_file = os.path.join(agent_dir, "TEAM.md")
            agents_dir = os.path.join(agent_dir, "agents")

            if os.path.exists(team_file) and os.path.isdir(agents_dir):
                self._load_team(dir_name, agent_dir, team_file, agents_dir)
                continue

            prompt_file = os.path.join(agent_dir, "PROMPT.md")
            if os.path.exists(prompt_file):
                with open(prompt_file, encoding="utf-8") as f:
                    content = f.read()

                frontmatter, body = extract_frontmatter(content)
                if frontmatter:
                    name = frontmatter.get("name", dir_name)
                    description = frontmatter.get("description", "")
                    template = {
                        "name": name,
                        "description": description,
                        "workspace": agent_dir
                    }
                    self.templates[name] = template
                    logger.debug(f"加载子代理模板: {name}")
            else:
                logger.warning(f"Subagent missing {prompt_file}")

    def set_event_callbacks(self, on_tool_event, on_token):
        """设置父代理的事件回调，供子代理转发事件到 Web UI"""
        self._parent_on_tool_event = on_tool_event
        self._parent_on_token = on_token

    def _build_callback_prefix(self, agent_name, agent_type):
        """构建回调包装函数，在事件数据中追加子代理身份信息"""
        parent_tool_event = self._parent_on_tool_event
        # round tracking: detect new LLM thinking rounds for separate bubbles
        state = {"in_round": False, "had_tools": False}

        async def wrapped_tool_event(event_type, data):
            if not parent_tool_event:
                return
            if event_type in ("tool_start", "tool_result"):
                enriched_type = "subagent_tool_" + event_type.split("_")[1]
                if event_type == "tool_result":
                    state["had_tools"] = True
            else:
                enriched_type = event_type
            enriched_data = {"agent_name": agent_name, "agent_type": agent_type, **(data or {})}
            with contextlib.suppress(Exception):
                await parent_tool_event(enriched_type, enriched_data)

        async def wrapped_token(token):
            if not parent_tool_event:
                return
            # If we had tools since last round, start a new bubble
            if state["had_tools"] or not state["in_round"]:
                state["had_tools"] = False
                state["in_round"] = True
                with contextlib.suppress(Exception):
                    await parent_tool_event("subagent_round_start", {
                        "agent_name": agent_name,
                        "agent_type": agent_type,
                    })
            with contextlib.suppress(Exception):
                await parent_tool_event("subagent_token", {
                    "agent_name": agent_name,
                    "agent_type": agent_type,
                    "content": token,
                })

        return wrapped_tool_event, wrapped_token

    def _inject_agent_callbacks(self, agent, template_name, agent_type):
        """为子代理实例注入包装后的事件回调"""
        if not self._parent_on_tool_event and not self._parent_on_token:
            return
        wrapped_tool_event, wrapped_token = self._build_callback_prefix(
            agent_name=template_name or agent.name or "unknown",
            agent_type=agent_type
        )
        agent.on_tool_event = wrapped_tool_event
        agent.on_token = wrapped_token

    def _load_team(self, dir_name: str, agent_dir: str, team_file: str, agents_dir: str):
        """加载团队配置和成员模板"""
        with open(team_file, encoding="utf-8") as f:
            content = f.read()

        frontmatter, body = extract_frontmatter(content)
        if not frontmatter:
            return

        name = frontmatter.get("name", dir_name)

        raw_members = frontmatter.get("members", [])
        team_roles = ""
        if isinstance(raw_members, list):
            role_lines = []
            for m in raw_members:
                if isinstance(m, dict):
                    role_lines.append(f"- {m.get('name', '')}: {m.get('role', '')}")
                else:
                    role_lines.append(f"- {m}")
            team_roles = "\n".join(role_lines)

        prompt_file = os.path.join(agent_dir, "PROMPT.md")
        prompt_body = ""
        if os.path.exists(prompt_file):
            with open(prompt_file, encoding="utf-8") as f:
                _, prompt_body = extract_frontmatter(f.read())

        config = {
            "name": name,
            "description": frontmatter.get("description", ""),
            "leader": frontmatter.get("leader", ""),
            "workspace": agent_dir,
            "team_body": body,
            "team_roles": team_roles,
            "leader_prompt": prompt_body,
            "dir_name": dir_name,
        }
        self._team_configs[name] = config

        members: dict[str, dict[str, Any]] = {}
        for member_name in os.listdir(agents_dir):
            member_path = os.path.join(agents_dir, member_name)
            if not os.path.isdir(member_path):
                continue
            template = self.get_team_member_template(dir_name, member_name)
            if template:
                members[member_name] = template
        self._team_members[name] = members

        self.templates[name] = {
            "name": name,
            "description": config["description"],
            "workspace": agent_dir,
            "is_team": True,
        }
        logger.info(f"加载团队: {name}, 成员: {list(members.keys())}")

    def scan_teams(self) -> dict[str, list[str]]:
        """
        扫描 workspace/agents/ 下的团队目录。

        识别规则: 目录包含 TEAM.md 和 agents/ 子目录则视为团队。

        Returns:
            {team_name: [member_names]}
        """
        result: dict[str, list[str]] = {}
        if not self.base_dir or not os.path.exists(self.base_dir):
            return result

        for dir_name in os.listdir(self.base_dir):
            agent_dir = os.path.join(self.base_dir, dir_name)
            if not os.path.isdir(agent_dir):
                continue

            team_file = os.path.join(agent_dir, "TEAM.md")
            agents_dir = os.path.join(agent_dir, "agents")
            if not (os.path.exists(team_file) and os.path.isdir(agents_dir)):
                continue

            members = []
            for member_name in os.listdir(agents_dir):
                member_path = os.path.join(agents_dir, member_name)
                if os.path.isdir(member_path):
                    members.append(member_name)

            if members:
                result[dir_name] = members
                logger.debug(f"发现团队: {dir_name}, 成员: {members}")

        return result

    def get_team_member_template(self, team_name: str, member_name: str) -> dict[str, Any] | None:
        """
        获取团队中某个成员的模板数据。

        Args:
            team_name: 团队目录名
            member_name: 成员目录名

        Returns:
            {name, description, workspace} 或 None
        """
        member_dir = os.path.join(self.base_dir, team_name, "agents", member_name)
        prompt_file = os.path.join(member_dir, "PROMPT.md")
        if not os.path.exists(prompt_file):
            return None

        with open(prompt_file, encoding="utf-8") as f:
            content = f.read()

        frontmatter, _ = extract_frontmatter(content)
        if not frontmatter:
            return None

        return {
            "name": frontmatter.get("name", member_name),
            "description": frontmatter.get("description", ""),
            "workspace": member_dir
        }

    async def _create_team_subagent(
        self,
        team_name: str,
        member_name: str,
        client=None,
        parent_agent=None,
    ) -> "Agent":
        """
        创建团队成员的子代理实例（不使用 templates 注册）。

        Args:
            team_name: 团队目录名
            member_name: 成员目录名
            client: LLM客户端
            parent_agent: 父代理

        Returns:
            Agent 实例
        """
        from agent import Agent

        cache_key = f"{team_name}/{member_name}"
        if cache_key not in self._team_member_cache:
            template_data = self.get_team_member_template(team_name, member_name)
            if not template_data:
                raise ValueError(f"团队 {team_name} 中未找到成员 {member_name}")
            self._team_member_cache[cache_key] = template_data

        workspace = self._team_member_cache[cache_key]["workspace"]

        agent = Agent(
            workspace=workspace,
            client=client or self._client,
            parent_agent=parent_agent or self._parent_agent,
        )
        if parent_agent or self._parent_agent:
            agent.plugin_manager = (parent_agent or self._parent_agent).plugin_manager

        await agent.initialize()
        # 注入事件回调（用于 Web UI 展示团队工具调用和流式输出）
        self._inject_agent_callbacks(agent, template_name=f"{team_name}/{member_name}", agent_type="team")
        return agent

    async def run_team_agent(
        self,
        team_name: str,
        member_name: str,
        task: str,
        client=None,
        parent_agent=None,
    ) -> str:
        """
        运行团队中的某个成员 agent。

        1. Create/get cached team member template
        2. Create Agent instance directly (bypassing templates/run_subagent)
        3. Run the task
        4. Cleanup and return result

        Args:
            team_name: 团队目录名
            member_name: 成员目录名
            task: 任务内容
            client: LLM客户端
            parent_agent: 父代理

        Returns:
            执行结果字符串，失败时以 "ERROR:" 开头
        """
        try:
            agent = await self._create_team_subagent(
                team_name, member_name,
                client=client,
                parent_agent=parent_agent,
            )
            try:
                agent_result = await agent.run(task)
                if agent_result.status == "failed":
                    return f"ERROR: 团队子代理 {member_name} 执行失败: {agent_result.result}"
                return agent_result.result
            finally:
                await agent.cleanup()
        except ValueError as e:
            return f"ERROR: {e}"
        except Exception as e:
            logger.error(f"团队子代理 {member_name} 执行异常: {e}")
            return f"ERROR: 团队子代理 {member_name} 执行异常: {e}"

    def get_subagent_prompt(self) -> str:
        """生成子代理列表提示词（包含个人和团队）"""
        if not self.templates:
            return "没有可用的子代理"

        lines = ["\n\n## 【SubAgent列表】\n"]
        for key, template_data in self.templates.items():
            if "/" in key:
                continue
            tag = " [团队]" if template_data.get("is_team") else ""
            lines.append(f"名称：[{template_data['name']}]{tag}\n")
            lines.append(f"描述：{template_data['description']}\n")
        lines.append("\n通过subagent工具调用激活\n")
        return "\n".join(lines)

    def list_templates(self) -> list[str]:
        """列出所有子代理名称（排除团队成员）"""
        return [k for k in self.templates if "/" not in k]

    def get_template(self, name: str) -> dict[str, Any] | None:
        """获取指定子代理模板"""
        return self.templates.get(name)

    def is_team(self, name: str) -> bool:
        """判断指定名称是否为团队"""
        return name in self._team_configs

    def list_active_subagents(self) -> list[dict[str, Any]]:
        """列出所有活跃的子代理"""
        result = []
        for session_id, instance in list(self._active_subagents.items()):
            result.append({
                "session_id": session_id,
                "template": instance.template,
                "task_count": instance.task_count,
                "last_used": instance.last_used
            })
        return result

    def get_active_subagent(self, session_id: str) -> SubagentInstance | None:
        """获取活跃的子代理实例"""
        return self._active_subagents.get(session_id)

    def get_subagent_by_name(self, name: str) -> SubagentInstance | None:
        """通过模板名获取活跃的子代理"""
        session_id = self._name_to_session.get(name)
        if session_id:
            return self._active_subagents.get(session_id)
        return None

    def get_sessions_by_template(self, template: str) -> list[dict[str, Any]]:
        """
        获取指定模板的所有活跃session

        Args:
            template: 模板名称

        Returns:
            该模板的所有session信息列表
        """
        sessions = []
        for session_id, instance in list(self._active_subagents.items()):
            if instance.template == template:
                sessions.append({
                    "session_id": session_id,
                    "template": instance.template,
                    "task_count": instance.task_count,
                    "created_at": instance.created_at,
                    "last_used": instance.last_used,
                    "agent_id": instance.agent.agent_id
                })
        return sessions

    def get_all_sessions(self) -> dict[str, list[dict[str, Any]]]:
        """
        获取所有模板的session分组

        Returns:
            按模板名分组的session字典
        """
        grouped = {}
        for session_id, instance in list(self._active_subagents.items()):
            template = instance.template or "未命名"
            if template not in grouped:
                grouped[template] = []
            grouped[template].append({
                "session_id": session_id,
                "task_count": instance.task_count,
                "created_at": instance.created_at,
                "last_used": instance.last_used,
                "agent_id": instance.agent.agent_id
            })
        return grouped

    async def get_or_create_subagent(
        self,
        template: str = "",
        name: str = "",
        session_id: str = "",
        system_prompt: str = "",
        tools: list[str] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        client=None,
        parent_agent=None
    ) -> tuple:
        """
        获取或创建子代理

        Args:
            template: 模板名称
            name: 子代理名称
            session_id: 会话ID（用于复用已有子代理）
            system_prompt: 系统提示词
            tools: 工具列表
            mcp_servers: MCP服务器配置
            client: LLM客户端
            parent_agent: 父代理

        Returns:
            (SubagentInstance, is_new)
        """
        from agent import Agent

        # 保存客户端和父代理引用
        if client:
            self._client = client
        if parent_agent:
            self._parent_agent = parent_agent

        # 优先通过 session_id 查找
        async with self._lock:
            if session_id and session_id in self._active_subagents:
                instance = self._active_subagents[session_id]
                instance.last_used = time.time()
                logger.info(f"复用子代理: template={instance.template}, session={session_id}")
                self._inject_agent_callbacks(instance.agent, template_name=instance.template, agent_type="subagent")
                return instance, False

        # 通过模板名查找
        template_name = template or name
        async with self._lock:
            if template_name and template_name in self._name_to_session:
                existing_session = self._name_to_session[template_name]
                if existing_session in self._active_subagents:
                    instance = self._active_subagents[existing_session]
                    instance.last_used = time.time()
                    logger.info(f"复用子代理: template={template_name}, session={existing_session}")
                    self._inject_agent_callbacks(instance.agent, template_name=template_name, agent_type="subagent")
                    return instance, False

        # 创建新的子代理（不持锁，因为初始化耗时）
        template_data = self.templates.get(template_name)
        workspace = template_data["workspace"] if template_data else None

        agent = Agent(
            workspace=workspace,
            client=self._client or client,
            parent_agent=self._parent_agent or parent_agent
        )

        if self._parent_agent or parent_agent:
            agent.plugin_manager = (self._parent_agent or parent_agent).plugin_manager

        await agent.initialize()

        # 设置名称和提示词
        if not template_data:
            agent.name = name
            agent.system_prompt = system_prompt

        # 注入事件回调（用于 Web UI 展示工具调用和流式输出）
        self._inject_agent_callbacks(agent, template_name=template_name, agent_type="subagent")

        # 创建实例并注册（持锁）
        new_session_id = session_id or str(uuid.uuid4())[:8]
        instance = SubagentInstance(
            agent=agent,
            template=template_name,
            session_id=new_session_id
        )

        async with self._lock:
            self._active_subagents[new_session_id] = instance
            if template_name:
                self._name_to_session[template_name] = new_session_id

        logger.info(f"创建新子代理: template={template_name}, session={new_session_id}")
        return instance, True

    async def run_subagent(
        self,
        task: str,
        template: str = "",
        name: str = "",
        session_id: str = "",
        system_prompt: str = "",
        tools: list[str] | None = None,
        mcp_servers: list[str, Any] | None = None,
        client=None,
        parent_agent: "Agent" = None,
        keep_alive: bool = True
    ) -> "AgentResult":
        """
        运行子代理（统一入口：个人子代理和团队均通过此方法调用）

        Args:
            task: 任务内容
            template: 模板名称
            name: 子代理名称
            session_id: 会话ID（用于复用）
            system_prompt: 系统提示词
            tools: 工具列表
            mcp_servers: MCP服务器配置
            client: LLM客户端
            parent_agent: 父代理
            keep_alive: 是否保持子代理存活（默认True）

        Returns:
            AgentResult
        """
        from agent import AgentResult

        # 保存引用
        if client:
            self._client = client
        if parent_agent:
            self._parent_agent = parent_agent

        template_name = template or name

        # 团队路由：如果是团队，使用 TeamOrchestrator
        if self.is_team(template_name):
            return await self._run_team_orchestrator(task, template_name)

        # 个人子代理：原有逻辑
        try:
            instance, is_new = await self.get_or_create_subagent(
                template=template,
                name=name,
                session_id=session_id,
                system_prompt=system_prompt,
                tools=tools,
                mcp_servers=mcp_servers,
                client=client,
                parent_agent=parent_agent
            )

            # 执行任务
            try:
                result = await instance.agent.run(task)
                instance.task_count += 1
                instance.last_used = time.time()
            except Exception as e:
                result = AgentResult(
                    agent_id=instance.agent.agent_id,
                    status="failed",
                    result=f"子代理执行错误: {e}"
                )
                logger.error(f"子代理执行错误: {e}")

            # 如果不需要保持存活，则清理
            if not keep_alive:
                await self.cleanup_subagent(instance.session_id)

            return result

        except Exception as e:
            logger.error(f"子代理创建/执行错误: {e}")
            return AgentResult(
                agent_id="",
                status="failed",
                result=f"子代理错误: {e}"
            )

    async def _run_team_orchestrator(self, task: str, team_name: str) -> "AgentResult":
        """通过 TeamOrchestrator 运行团队"""
        from agent import AgentResult
        from team.orchestrator import TeamOrchestrator

        config = self._team_configs.get(team_name)
        members = self._team_members.get(team_name, {})

        if not config or not members:
            return AgentResult(
                agent_id="",
                status="failed",
                result=f"团队 {team_name} 配置无效或无成员"
            )

        logger.info(f"启动团队编排: {team_name}, 成员: {list(members.keys())}")

        orchestrator = TeamOrchestrator(
            team_name=team_name,
            team_config=config,
            members=members,
            subagent_manager=self,
            llm_client=self._client,
            memory_manager=getattr(self._parent_agent, "memory", None) if self._parent_agent else None,
        )
        try:
            result = await orchestrator.run(task)
            status = "completed" if not result.startswith("ERROR:") else "failed"
            return AgentResult(
                agent_id=f"team:{team_name}",
                status=status,
                result=result,
            )
        except Exception as e:
            logger.error(f"团队编排异常: {e}")
            return AgentResult(
                agent_id=f"team:{team_name}",
                status="failed",
                result=f"团队执行错误: {e}",
            )

    async def cleanup_subagent(self, session_id: str):
        """清理指定的子代理"""
        instance = None
        async with self._lock:
            if session_id not in self._active_subagents:
                return
            instance = self._active_subagents.pop(session_id)

            # 清理名称映射
            if instance.template in self._name_to_session and self._name_to_session[instance.template] == session_id:
                del self._name_to_session[instance.template]

        await instance.agent.cleanup()
        logger.info(f"清理子代理: template={instance.template}, session={session_id}")

    async def cleanup_all(self):
        """清理所有活跃的子代理"""
        async with self._lock:
            session_ids = list(self._active_subagents.keys())
        for session_id in session_ids:
            await self.cleanup_subagent(session_id)
        logger.info(f"已清理所有子代理，共 {len(session_ids)} 个")

    def get_stats(self) -> dict[str, Any]:
        """获取子代理统计信息"""
        return {
            "templates_count": len(self.templates),
            "team_count": len(self._team_configs),
            "active_count": len(self._active_subagents),
            "active_subagents": self.list_active_subagents()
        }

    def start_cleanup_task(self):
        """启动定期清理任务（清理过期子代理）"""
        if self._cleanup_task:
            return
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("子代理清理任务已启动")

    def stop_cleanup_task(self):
        """停止清理任务"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
            logger.info("子代理清理任务已停止")

    async def _cleanup_loop(self):
        """定期清理过期子代理"""
        while True:
            try:
                await asyncio.sleep(self.CLEANUP_INTERVAL)
            except asyncio.CancelledError:
                logger.info("子代理清理循环被取消")
                return
            try:
                await self._cleanup_expired()
            except asyncio.CancelledError:
                logger.info("子代理清理被取消")
                return
            except Exception as e:
                logger.error(f"子代理清理失败: {e}")

    async def _cleanup_expired(self):
        """清理过期的子代理"""
        now = time.time()
        expired_ids = []
        async with self._lock:
            for session_id, instance in self._active_subagents.items():
                if now - instance.last_used > self.SUBAGENT_TTL:
                    expired_ids.append(session_id)
        for session_id in expired_ids:
            await self.cleanup_subagent(session_id)
        if expired_ids:
            logger.info(f"已清理 {len(expired_ids)} 个过期子代理")

    def reload_templates(self):
        """重新加载所有子代理模板（热加载新增的模板）"""
        old_count = len(self.templates)
        self._load_all()
        new_count = len(self.templates)
        if new_count > old_count:
            logger.info(f"热加载子代理模板: 新增 {new_count - old_count} 个，当前共 {new_count} 个")
        return new_count
