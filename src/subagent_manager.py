"""
子代理管理器模块

支持子代理持久化，保持上下文连续性
"""
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
    """子代理管理器 - 支持持久化和会话复用"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.templates: dict[str, dict[str, Any]] = {}
        self._active_subagents: dict[str, SubagentInstance] = {}  # session_id -> SubagentInstance
        self._name_to_session: dict[str, str] = {}  # template/name -> session_id
        self._team_member_cache: dict[str, dict[str, Any]] = {}
        self._client = None
        self._parent_agent = None
        self._load_all()

    def _load_all(self):
        """加载所有子代理模板（跳过团队目录）"""
        if not self.base_dir or not os.path.exists(self.base_dir):
            logger.warning(f"Subagent directory not found: {self.base_dir}")
            return

        for dir_name in os.listdir(self.base_dir):
            agent_dir = os.path.join(self.base_dir, dir_name)
            if not os.path.isdir(agent_dir):
                continue

            # 跳过团队目录（包含 TEAM.md）
            if os.path.exists(os.path.join(agent_dir, "TEAM.md")):
                logger.debug(f"跳过团队目录: {dir_name}")
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

    def scan_teams(self) -> dict[str, list[str]]:
        """
        扫描 workspace/agents/ 下的团队目录。

        识别规则: 目录包含 TEAM.md 和 members/ 子目录则视为团队。

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
            members_dir = os.path.join(agent_dir, "members")
            if not (os.path.exists(team_file) and os.path.isdir(members_dir)):
                continue

            members = []
            for member_name in os.listdir(members_dir):
                member_path = os.path.join(members_dir, member_name)
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
        member_dir = os.path.join(self.base_dir, team_name, "members", member_name)
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

        template_data = self._team_member_cache[cache_key]
        workspace = template_data["workspace"]

        agent = Agent(
            workspace=workspace,
            client=client or self._client,
            parent_agent=parent_agent or self._parent_agent,
        )
        if parent_agent or self._parent_agent:
            agent.plugin_manager = (parent_agent or self._parent_agent).plugin_manager

        await agent.initialize()
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
        """生成子代理列表提示词（排除团队成员模板）"""
        if not self.templates:
            return "没有可用的子代理"

        lines = ["\n\n## 【SubAgent列表】\n"]
        for key, template_data in self.templates.items():
            if "/" in key:
                continue
            lines.append(f"名称：[{template_data['name']}]\n")
            lines.append(f"描述：{template_data['description']}\n")
        lines.append("\n通过subagent工具调用激活\n")
        return "\n".join(lines)

    def list_templates(self) -> list[str]:
        """列出所有子代理名称（排除团队成员）"""
        return [k for k in self.templates if "/" not in k]

    def get_template(self, name: str) -> dict[str, Any] | None:
        """获取指定子代理模板"""
        return self.templates.get(name)

    def list_active_subagents(self) -> list[dict[str, Any]]:
        """列出所有活跃的子代理"""
        result = []
        for session_id, instance in self._active_subagents.items():
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
        for session_id, instance in self._active_subagents.items():
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
        for session_id, instance in self._active_subagents.items():
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
        if session_id and session_id in self._active_subagents:
            instance = self._active_subagents[session_id]
            instance.last_used = time.time()
            logger.info(f"复用子代理: template={instance.template}, session={session_id}")
            return instance, False

        # 通过模板名查找
        template_name = template or name
        if template_name and template_name in self._name_to_session:
            existing_session = self._name_to_session[template_name]
            if existing_session in self._active_subagents:
                instance = self._active_subagents[existing_session]
                instance.last_used = time.time()
                logger.info(f"复用子代理: template={template_name}, session={existing_session}")
                return instance, False

        # 创建新的子代理
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

        # 创建实例
        new_session_id = session_id or str(uuid.uuid4())[:8]
        instance = SubagentInstance(
            agent=agent,
            template=template_name,
            session_id=new_session_id
        )

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
        mcp_servers: list[dict[str, Any]] | None = None,
        client=None,
        parent_agent: "Agent" = None,
        keep_alive: bool = True
    ) -> "AgentResult":
        """
        运行子代理

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

    async def cleanup_subagent(self, session_id: str):
        """清理指定的子代理"""
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
        session_ids = list(self._active_subagents.keys())
        for session_id in session_ids:
            await self.cleanup_subagent(session_id)
        logger.info(f"已清理所有子代理，共 {len(session_ids)} 个")

    def get_stats(self) -> dict[str, Any]:
        """获取子代理统计信息"""
        return {
            "templates_count": len(self.templates),
            "active_count": len(self._active_subagents),
            "active_subagents": self.list_active_subagents()
        }

    def reload_templates(self):
        """重新加载所有子代理模板（热加载新增的模板）"""
        old_count = len(self.templates)
        self._load_all()
        new_count = len(self.templates)
        if new_count > old_count:
            logger.info(f"热加载子代理模板: 新增 {new_count - old_count} 个，当前共 {new_count} 个")
        return new_count
