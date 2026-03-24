import logging
import uuid
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime
import os
import re
import json

logger = logging.getLogger("agent.subagent")


@dataclass
class AgentResult:
    agent_id: str
    status: str
    result: str
    iterations: int
    error: Optional[str] = None
    completed_at: str = field(
        default_factory=lambda: datetime.now().isoformat())


class Agent:
    def __init__(
        self,
        workspace: str,
        client,
        parent_agent: "Agent" = None
    ):
        self.workspace = workspace
        self.client = client
        self.parent_agent = parent_agent
        self.agent_id = str(uuid.uuid4())[:8]

        self.name = ""
        self.description = ""
        self.system_prompt = ""
        self.tools: List[str] = []
        self.max_iterations = 50

        self.tool_registry = None
        self.mcp = None
        self.skill_manager = None
        self.subagent_manager = None
        self.session_manager = None

        self.messages: List[Dict[str, Any]] = []
        self.status = "pending"
        self.result: Optional[str] = None
        self.error: Optional[str] = None
        self.iterations = 0

    async def initialize(self, tool_registry=None, session_manager=None):
        self._load_system_prompt()
        self._init_tools(tool_registry)
        self._init_skills()
        self._load_mcp_servers()
        await self._init_mcp()

        if session_manager:
            self.session_manager = session_manager
        else:
            from agent_session import AgentSessionManager
            self.session_manager = AgentSessionManager()

        self._init_subagents()
        logger.info(f"Agent [{self.name}] initialized")

    def _load_system_prompt(self):
        prompt_file = os.path.join(self.workspace, "PROMPT.md")

        if not os.path.exists(prompt_file):
            logger.warning(f"No PROMPT.md found in {self.workspace}")
            self.name = os.path.basename(self.workspace)
            return

        with open(prompt_file, "r", encoding="utf-8") as f:
            content = f.read()

        frontmatter, body = self._extract_frontmatter(content)

        if frontmatter:
            self.name = frontmatter.get(
                "name", os.path.basename(self.workspace))
            self.description = frontmatter.get("description", "")
            if isinstance(self.description, str):
                self.description = self.description.strip()

            tools = frontmatter.get("tools", [])
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            self.tools = tools

            self.max_iterations = frontmatter.get("max_iterations", 50)

        self.system_prompt = body.strip() if body else ""
        logger.debug(f"Agent [{self.name}] 读取prompt {prompt_file}")
        logger.info(f"Agent [{self.name}] prompt initialized")

    def _init_tools(self, parent_tool_registry=None):
        from tools import ToolRegistry, TodoTool, FileTool

        if parent_tool_registry and not self.tools:
            self.tool_registry = parent_tool_registry
        else:
            self.tool_registry = ToolRegistry()
            self.tool_registry.register_tool(TodoTool())
            self.tool_registry.register_tool(FileTool())

        logger.debug(f"Agent [{self.name}] tools initialized")
        logger.info(
            f"✓ 已注册 {len(self.tool_registry.list_tools())} 个工具: {[self.tool_registry.list_tools()]}")

    def _init_skills(self):
        skills_dir = os.path.join(self.workspace, "skills")
        if os.path.exists(skills_dir):
            from skills import SkillManager
            self.skill_manager = SkillManager(skills_dir)
            self.system_prompt = self.system_prompt + \
                self.skill_manager.get_skills_prompt()
            logger.debug(
                f"Agent [{self.name}] loaded skills from {skills_dir}")
        logger.info(
            f"✓ 已加载 {len(self.skill_manager.list_skills())} 个技能: {[self.skill_manager.list_skills()]}")

    def _load_mcp_servers(self):
        mcp_file = os.path.join(self.workspace, "mcp_servers.json")
        self.mcp_configs = []

        if os.path.exists(mcp_file):
            try:
                with open(mcp_file, "r", encoding="utf-8") as f:
                    self.mcp_configs = json.load(f)
                logger.info(
                    f"Agent [{self.name}] loaded {len(self.mcp_configs)} MCP configs")
            except Exception as e:
                logger.error(f"Failed to load mcp_servers.json: {e}")

    def _extract_frontmatter(self, content: str) -> tuple:
        pattern = r"^---\s*\n(.*?)\n---\s*\n?(.*)$"
        match = re.match(pattern, content, re.DOTALL)

        if not match:
            return {}, content

        frontmatter_str = match.group(1)
        body = match.group(2)

        import yaml
        try:
            frontmatter = yaml.safe_load(frontmatter_str) or {}
        except yaml.YAMLError as e:
            logger.error(f"YAML parse error: {e}")
            return {}, content

        return frontmatter, body

    async def _init_mcp(self):
        if self.mcp_configs:
            from mcps import MCPManager
            self.mcp = MCPManager("")
            for config in self.mcp_configs:
                await self.mcp.connect_server(config)
            logger.info(
                f"Agent [{self.name}] initialized {len(self.mcp_configs)} MCP servers")

    def _init_subagents(self):
        agents_dir = os.path.join(self.workspace, "agents")
        if os.path.exists(agents_dir):
            self.subagent_manager = SubagentManager(agents_dir)
            self.system_prompt = self.system_prompt + \
                self.subagent_manager.get_subagent_prompt()
            logger.info(
                f"Agent [{self.name}] loaded subagents from {agents_dir}")

    @property
    def tool_defs(self) -> List[Dict[str, Any]]:
        tools = []

        if self.tool_registry:
            tools.extend(self.tool_registry.get_tool_definitions())

        if self.mcp:
            tools.extend(self.mcp.tool_defs)

        if self.skill_manager:
            tools.extend(self.skill_manager.get_tool_definitions())

        if self.tools:
            tool_names = set(self.tools)
            tools = [t for t in tools if t.get(
                "function", {}).get("name") in tool_names]

        return tools

    async def run(self, task: str, session=None) -> AgentResult:
        self.status = "running"
        self.messages = []

        if self.system_prompt:
            self.messages.append(
                {"role": "system", "content": self.system_prompt})

        self.messages.append({"role": "user", "content": task})

        logger.info(
            f"Agent [{self.name}] ({self.agent_id}) started: {task[:50]}...")

        try:
            for i in range(self.max_iterations):
                self.iterations = i + 1
                logger.debug(f"Agent [{self.name}] iteration {i + 1}")

                response = await self._think()
                msg = response.get("message", {})

                self.messages.append({
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": msg.get("tool_calls")
                })

                if msg.get("tool_calls"):
                    for tc in msg.get("tool_calls", []):
                        func_name = tc.get("function", {}).get("name", "")
                        func_args = tc.get("function", {}).get("arguments", {})

                        if isinstance(func_args, str):
                            try:
                                func_args = json.loads(func_args)
                            except:
                                func_args = {}

                        logger.info(
                            f"Agent [{self.name}] -> tool: {func_name}")
                        result = await self._execute_tool(func_name, func_args)
                        logger.info(f"Agent [{self.name}] <- {func_name} done")

                        self.messages.append({
                            "role": "tool",
                            "content": result,
                            "tool_call_id": tc.get("id", "")
                        })

                    continue

                if msg.get("content"):
                    self.status = "completed"
                    self.result = msg.get("content")
                    logger.info(
                        f"Agent [{self.name}] ({self.agent_id}) completed")
                    break
            else:
                self.status = "max_iterations"
                self.result = "达到最大迭代次数"
                logger.warning(f"Agent [{self.name}] max iterations reached")

        except Exception as e:
            self.status = "failed"
            self.error = str(e)
            logger.error(f"Agent [{self.name}] failed: {e}")

        return AgentResult(
            agent_id=self.agent_id,
            status=self.status,
            result=self.result or "",
            iterations=self.iterations,
            error=self.error
        )

    async def _think(self) -> Dict[str, Any]:
        try:
            response = self.client.chat(
                self.messages,
                self.tool_defs,
                stream=False
            )

            choice = response.choices[0]
            msg = choice.message

            tool_calls = None
            if msg.tool_calls:
                tool_calls = []
                for tc in msg.tool_calls:
                    tool_calls.append({
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    })

            return {
                "message": {
                    "content": msg.content,
                    "tool_calls": tool_calls
                }
            }
        except Exception as e:
            logger.error(f"Agent [{self.name}] think error: {e}")
            return {"message": {"content": f"思考出错: {e}"}}

    async def _execute_tool(self, name: str, args: Dict) -> str:
        try:
            if self.tool_registry and self.tool_registry.has_tool(name):
                return await self.tool_registry.execute(name, args)

            if self.skill_manager and name == "execute_skill":
                return await self.skill_manager.execute_tool(name, args)

            if self.mcp:
                return await self.mcp.call_tool(name, args)

            if name == "subagent" and self.subagent_manager:
                return await self._execute_subagent(args)

            return f"工具 {name} 不存在"
        except Exception as e:
            return f"工具执行错误: {e}"

    async def _execute_subagent(self, args: Dict) -> str:
        task = args.get("task")
        if not task:
            return json.dumps({"success": False, "error": "缺少task参数"}, ensure_ascii=False)

        result, agent_name = await self.subagent_manager.run_subagent(
            task=task,
            template=args.get("template", ""),
            name=args.get("name", ""),
            system_prompt=args.get("system_prompt", ""),
            tools=args.get("tools"),
            max_iterations=args.get("max_iterations", 50),
            mcp_servers=args.get("mcp_servers"),
            client=self.client,
            tool_registry=self.tool_registry,
            parent_agent=self
        )

        return json.dumps({
            "success": result.status == "completed",
            "agent_id": result.agent_id,
            "name": agent_name,
            "status": result.status,
            "result": result.result,
            "iterations": result.iterations,
            "error": result.error
        }, ensure_ascii=False)

    async def cleanup(self):
        if self.mcp:
            await self.mcp.close()
            logger.info(f"Agent [{self.name}] cleaned up MCP")


class SubagentManager:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.templates: Dict[str, Dict[str, Any]] = {}
        self._load_all()

    def _load_all(self):
        if not self.base_dir or not os.path.exists(self.base_dir):
            logger.warning(f"Subagent directory not found: {self.base_dir}")
            return

        for name in os.listdir(self.base_dir):
            agent_dir = os.path.join(self.base_dir, name)
            if not os.path.isdir(agent_dir):
                continue

            template = {
                "name": name,
                "workspace": agent_dir
            }
            self.templates[name] = template
            logger.info(f"Loaded subagent template: {name}")

    def get_subagent_prompt(self) -> str:
        if not self.templates:
            return "没有可用的子代理"

        lines = ["\n## 【SubAgent列表】\n"]
        for name in self.templates:
            lines.append(f"[名称：{name}]")
            lines.append("")
        return "\n".join(lines) + "\n通过subagent工具调用激活\n"

    def list_templates(self) -> List[Dict[str, Any]]:
        return [{"name": name, "workspace": t["workspace"]} for name, t in self.templates.items()]

    async def run_subagent(
        self,
        task: str,
        template: str = "",
        name: str = "",
        system_prompt: str = "",
        tools: Optional[List[str]] = None,
        max_iterations: int = 50,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        client=None,
        tool_registry=None,
        parent_agent: Agent = None
    ) -> tuple:
        template_name = template or name
        template_data = self.templates.get(template_name)

        workspace = template_data["workspace"] if template_data else None

        if not workspace:
            from tools import ToolRegistry, TodoTool, FileTool
            temp_dir = os.path.join(self.base_dir, name or "temp")
            os.makedirs(temp_dir, exist_ok=True)

            skill_content = "---\n"
            if name:
                skill_content += f"name: {name}\n"
            if system_prompt:
                pass
            skill_content += f"max_iterations: {max_iterations}\n"
            if tools:
                skill_content += f"tools: {tools}\n"
            skill_content += "---\n"
            if system_prompt:
                skill_content += system_prompt

            with open(os.path.join(temp_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(skill_content)

            if mcp_servers:
                with open(os.path.join(temp_dir, "mcp_servers.json"), "w", encoding="utf-8") as f:
                    json.dump(mcp_servers, f)
            else:
                with open(os.path.join(temp_dir, "mcp_servers.json"), "w", encoding="utf-8") as f:
                    json.dump([], f)

            workspace = temp_dir

        agent = Agent(
            workspace=workspace,
            client=client,
            parent_agent=parent_agent
        )

        if max_iterations != 50:
            agent.max_iterations = max_iterations
        if tools:
            agent.tools = tools

        await agent.initialize(tool_registry=tool_registry)

        try:
            result = await agent.run(task)
        finally:
            await agent.cleanup()

        return result, agent.name
