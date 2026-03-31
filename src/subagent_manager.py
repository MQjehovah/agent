"""
子代理管理器模块
"""
import os
import logging
from typing import Dict, Any, List, Optional, TYPE_CHECKING

from utils.frontmatter import extract_frontmatter

if TYPE_CHECKING:
    from agent import Agent, AgentResult

logger = logging.getLogger("agent.subagent")


class SubagentManager:
    """子代理模板管理器"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.templates: Dict[str, Dict[str, Any]] = {}
        self._load_all()

    def _load_all(self):
        """加载所有子代理模板"""
        if not self.base_dir or not os.path.exists(self.base_dir):
            logger.warning(f"Subagent directory not found: {self.base_dir}")
            return

        for dir_name in os.listdir(self.base_dir):
            agent_dir = os.path.join(self.base_dir, dir_name)
            if not os.path.isdir(agent_dir):
                continue

            prompt_file = os.path.join(agent_dir, "PROMPT.md")
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding="utf-8") as f:
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

    def get_subagent_prompt(self) -> str:
        """生成子代理列表提示词"""
        if not self.templates:
            return "没有可用的子代理"

        lines = ["\n\n## 【SubAgent列表】\n"]
        for template_data in self.templates.values():
            lines.append(f"名称：[{template_data['name']}]\n")
            lines.append(f"描述：{template_data['description']}\n")
        lines.append("\n通过subagent工具调用激活\n")
        return "\n".join(lines)

    def list_templates(self) -> List[str]:
        """列出所有子代理名称"""
        return list(self.templates.keys())

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        """获取指定子代理模板"""
        return self.templates.get(name)

    async def run_subagent(
        self,
        task: str,
        template: str = "",
        name: str = "",
        system_prompt: str = "",
        tools: Optional[List[str]] = None,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        client=None,
        parent_agent: "Agent" = None
    ) -> "AgentResult":
        """运行子代理"""
        from agent import Agent, AgentResult

        template_data = self.templates.get(template or name)
        workspace = template_data["workspace"] if template_data else None

        agent = Agent(
            workspace=workspace,
            client=client,
            parent_agent=parent_agent
        )

        if parent_agent:
            agent.plugin_manager = parent_agent.plugin_manager

        await agent.initialize()

        # 没有模板数据则直接用预设参数初始化
        if not template_data:
            agent.name = name
            agent.system_prompt = system_prompt

        try:
            result = await agent.run(task)
        except Exception as e:
            result = AgentResult(
                agent_id=agent.agent_id,
                status="failed",
                result=f"子代理执行错误: {e}"
            )
            logger.error(f"子代理执行错误: {e}")
        finally:
            await agent.cleanup()

        return result