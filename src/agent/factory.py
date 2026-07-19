"""
AgentFactory — 从 template 创建 Agent 实例

系统中只有一种 Agent 类（agent/agent.py），区别仅在于：
  - config_dir 不同（指向不同 PROMPT.md）
  - role 不同（零号员工 / 代码工程师 / 测试工程师 ...）

AgentFactory 取代旧的 SubagentManager / SubagentInstance 概念。
"""
import logging
import os
from typing import Optional

from agent.core import Agent

logger = logging.getLogger("agent.factory")


class AgentFactory:
    """Agent 工厂 — 从 template 名称或配置目录创建 Agent 实例"""

    def __init__(self, config_dir: str, base_workspace: str = ""):
        self.config_dir = config_dir
        self.base_workspace = base_workspace or os.getcwd()
        self._team_configs: dict[str, dict] = {}
        self._team_member_cache: dict[str, dict] = {}

    def discover_templates(self) -> list[str]:
        """扫描 config/agents/ 发现所有可用 template 名称"""
        agents_dir = os.path.join(self.config_dir, "agents")
        if not os.path.isdir(agents_dir):
            return []
        return sorted([
            d for d in os.listdir(agents_dir)
            if os.path.isdir(os.path.join(agents_dir, d))
        ])

    async def create_agent(
        self,
        template: str = "",
        config_dir: str = "",
        name: str = "",
        client=None,
        parent_agent: Optional["Agent"] = None,
        session_id: str = "",
        system_prompt: str = "",
        tools: list[str] = None,
        mcp_servers: list = None,
        max_iterations: int = 0,
    ) -> tuple[Agent, str]:
        """创建 Agent 实例

        Args:
            template: template 名称（对应 config/agents/<name>/）
            config_dir: 直接指定配置目录（优先级高于 template）
            ...

        Returns:
            (Agent 实例, session_id)
        """
        from agent.subagent import SubagentManager

        mgr = SubagentManager(self.config_dir, self.base_workspace)
        mgr._parent_agent = parent_agent
        return await mgr.get_or_create_subagent(
            template=template,
            name=name,
            session_id=session_id,
            system_prompt=system_prompt,
            tools=tools,
            mcp_servers=mcp_servers,
            client=client,
            parent_agent=parent_agent,
            config_dir=config_dir,
        )

    async def create_team_member(
        self,
        team_name: str,
        role: str,
        client=None,
        parent_agent: Optional["Agent"] = None,
        max_iterations: int = 0,
    ) -> Agent:
        """创建团队成员 Agent"""
        from agent.subagent import SubagentManager

        mgr = SubagentManager(self.config_dir, self.base_workspace)
        mgr._parent_agent = parent_agent
        self._team_configs = mgr._team_configs
        self._team_member_cache = mgr._team_member_cache
        return await mgr._create_team_subagent(
            team_name, role,
            client=client, parent_agent=parent_agent,
            max_iterations=max_iterations,
        )
