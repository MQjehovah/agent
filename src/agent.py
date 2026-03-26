import logging
import uuid
import asyncio
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
    agent_name: str
    status: str
    result: str
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
        self.max_iterations = 100

        self.tool_registry = None
        self.mcp = None
        self.skill_manager = None
        self.subagent_manager = None
        self.session_manager = None
        self.plugin_manager = None
        self.memory = None
        self._background_tasks: set = set()

        self.status = "pending"
        self.result: Optional[str] = None

    async def initialize(self, session_id: str = None):
        self._load_system_prompt()
        self._init_tools()
        self._init_skills()
        await self._load_mcp_servers()

        from agent_session import AgentSessionManager
        self.session_manager = AgentSessionManager()

        self._init_subagents()
        self._init_memory(session_id) # session_id用于恢复会话

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

        self.system_prompt = body.strip() if body else ""
        logger.debug(f"Agent [{self.name}] 读取prompt {prompt_file}")
        logger.info(f"Agent [{self.name}] prompt initialized")

    def _init_tools(self):
        from tools import ToolRegistry, TodoTool, FileTool, SubagentTool, MemoryTool, ShellTool

        self.tool_registry = ToolRegistry()
        self.tool_registry.register_tool(TodoTool())
        self.tool_registry.register_tool(FileTool())
        self.tool_registry.register_tool(SubagentTool())
        self.tool_registry.register_tool(MemoryTool())
        self.tool_registry.register_tool(ShellTool())

        logger.debug(f"Agent [{self.name}] tools initialized")
        logger.info(
            f"Agent [] 已注册 {len(self.tool_registry.list_tools())} 个工具: {[self.tool_registry.list_tools()]}")

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
                f"Agent [] 已加载 {len(self.skill_manager.list_skills())} 个技能: {[self.skill_manager.list_skills()]}")

    async def _load_mcp_servers(self):
        mcp_file = os.path.join(self.workspace, "mcp_servers.json")
        self.mcp_configs = []

        if os.path.exists(mcp_file):
            try:
                with open(mcp_file, "r", encoding="utf-8") as f:
                    self.mcp_configs = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load mcp_servers.json: {e}")

        if self.mcp_configs:
            from mcps import MCPManager
            self.mcp = MCPManager("")
            for config in self.mcp_configs:
                if config.get("enabled", True):
                    await self.mcp.connect_server(config)
                else:
                    logger.info(f"跳过已禁用的 MCP server: {config.get('name', 'unnamed')}")

            connected = [c.get("name", "unnamed") for c in self.mcp_configs if c.get("enabled", True)]
            logger.info(
                f"Agent [{self.name}] 已连接 {len(connected)} MCP servers: {connected}")

    def _init_subagents(self):
        agents_dir = os.path.join(self.workspace, "agents")
        if os.path.exists(agents_dir):
            self.subagent_manager = SubagentManager(agents_dir)
            self.system_prompt = self.system_prompt + \
                self.subagent_manager.get_subagent_prompt()
            logger.debug(
                f"Agent [{self.name}] loaded subagents from {agents_dir}")
            logger.info(
                f"Agent [] 已加载 {len(self.subagent_manager.list_templates())} 个子代理: {[self.subagent_manager.list_templates()]}")

    def _init_memory(self, session_id: str = None):
        from memory import MemoryManager
        self.memory = MemoryManager(self.workspace)
        self.memory.start_session(session_id)

        memory_context = self.memory.load_memory("")
        if memory_context:
            self.system_prompt += f"\n\n## 【记忆上下文】\n{memory_context}"

        logger.info(f"Agent [{self.name}] memory initialized")

        memory_tool = self.tool_registry.get_tool("memory")
        if memory_tool and hasattr(memory_tool, 'set_memory_manager'):
            memory_tool.set_memory_manager(self.memory)

        logger.info(f"Agent [{self.name}] initialized")

    @property
    def tool_defs(self) -> List[Dict[str, Any]]:
        tools = []

        if self.tool_registry:
            tools.extend(self.tool_registry.get_tool_definitions())

        if self.mcp:
            tools.extend(self.mcp.tool_defs)

        if self.skill_manager:
            tools.extend(self.skill_manager.get_tool_definitions())

        if self.plugin_manager:
            for plugin in self.plugin_manager.plugins.values():
                if plugin.enabled:
                    tools.extend(plugin.get_tool_defs())

        return tools

    async def run(self, task: str, session_id: str = None) -> AgentResult:
        self.status = "running"

        from agent_session import AgentSession
        session = None
        if session_id and self.session_manager:
            session = await self.session_manager.get_session(session_id)
            if session:
                session.add_message("user", task)
                logger.info(
                    f"Agent [{self.name}] 复用session: {session_id}, 消息数: {len(session.messages)}")
            else:
                session = await self.session_manager.create_session(
                    session_id=session_id,
                    system_prompt=self.system_prompt
                )
                session.add_message("user", task)
                logger.info(f"Agent [{self.name}] 创建新session: {session_id}")

        if not session:
            session = AgentSession(
                session_id=session_id or "temp",
                system_prompt=self.system_prompt
            )
            if self.memory:
                memory_context = self.memory.load_memory(task)
                if memory_context:
                    session.messages.insert(0, {
                        "role": "system",
                        "content": f"## 【记忆上下文】\n{memory_context}"
                    })
            session.add_message("user", task)

        logger.info(
            f"Agent [{self.name}] ({self.agent_id}) started: {task[:50]}...")

        try:
            for i in range(self.max_iterations):
                logger.debug(f"Agent [{self.name}] [{session.session_id}] iteration {i + 1}")

                response = await self._think(session.messages)
                logger.debug(f"Agent [{self.name}] [{session.session_id}] think response: {response}")

                msg = response.get("message", {})

                session.messages.append({
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
                        logger.debug(
                            f"Agent [{self.name}] -> tool: {func_name} args: {func_args}")
                        result = await self._execute_tool(func_name, func_args)
                        logger.info(
                            f"Agent [{self.name}] <- tool: {func_name}")
                        logger.debug(
                            f"Agent [{self.name}] <- tool: {func_name} result: {result}")

                        session.messages.append({
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
            logger.error(f"Agent [{self.name}] failed: {e}")

        # if self.memory:
        #     self.memory.add_summary(task, self.result or "")
        #     self.memory.save_session()
        #     bg_task = asyncio.create_task(self._background_memory_extract())
        #     self._background_tasks.add(bg_task)
        #     bg_task.add_done_callback(self._background_tasks.discard)

        logger.debug(
            f"Agent [{self.name}] session完成: {session_id}, 消息数: {len(session.messages)}")

        return AgentResult(
            agent_id=self.agent_id,
            agent_name=self.name,
            status=self.status,
            result=self.result or "",
        )

    async def _background_memory_extract(self):
        try:
            await asyncio.sleep(0.1)
            if self.memory:
                self.memory.extract_daily(self.client)
                logger.debug(
                    f"Agent [{self.name}] memory extraction completed")
        except Exception as e:
            logger.error(f"Agent [{self.name}] memory extraction failed: {e}")

    async def _think(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            response = self.client.chat(
                messages,
                self.tool_defs,
                stream=False
            )

            choice = response.choices[0]
            msg = choice.message

            tool_calls = None
            if msg.tool_calls:
                tool_calls = []
                for tc in msg.tool_calls:
                    func_args = tc.function.arguments
                    if isinstance(func_args, str):
                        try:
                            json.loads(func_args)
                        except (json.JSONDecodeError, ValueError):
                            try:
                                func_args = json.dumps(func_args, ensure_ascii=False)
                            except Exception:
                                func_args = "{}"
                    elif isinstance(func_args, dict):
                        logger.warning(f"Agent [{self.name}] function.arguments is dict, converting to JSON string")
                        func_args = json.dumps(func_args, ensure_ascii=False)
                    else:
                        logger.warning(f"Agent [{self.name}] function.arguments is {type(func_args)}, set to empty object")
                        func_args = "{}"
                    tool_calls.append({
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": func_args
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
            if name == "subagent" and self.subagent_manager:
                return await self._execute_subagent(args)

            if self.tool_registry and self.tool_registry.has_tool(name):
                return await self.tool_registry.execute(name, args)

            if self.skill_manager and name == "execute_skill":
                return await self.skill_manager.execute_tool(name, args)

            if self.mcp:
                return await self.mcp.call_tool(name, args)

            if self.plugin_manager:
                for plugin in self.plugin_manager.plugins.values():
                    logger.debug(f"检查插件 {plugin.name}, enabled={plugin.enabled}")
                    if plugin.enabled:
                        tool_defs = plugin.get_tool_defs()
                        logger.debug(f"插件 {plugin.name} 工具定义: {[t.get('function', {}).get('name') for t in tool_defs]}")
                        if any(t.get("function", {}).get("name") == name for t in tool_defs):
                            logger.info(f"执行插件工具: {plugin.name}.{name}")
                            return await plugin.execute_tool(name, args)

            return f"工具 {name} 不存在"
        except Exception as e:
            return f"工具执行错误: {e}"

    async def _execute_subagent(self, args: Dict) -> str:
        task = args.get("task")
        if not task:
            return json.dumps({"success": False, "error": "缺少task参数"}, ensure_ascii=False)

        result = await self.subagent_manager.run_subagent(
            task=task,
            template=args.get("template", ""),
            name=args.get("name", ""),
            system_prompt=args.get("system_prompt", ""),
            tools=args.get("tools"),
            mcp_servers=args.get("mcp_servers"),
            client=self.client,
            parent_agent=self
        )

        return json.dumps({
            "success": result.status == "completed",
            "agent_id": result.agent_id,
            "name": result.agent_name,
            "status": result.status,
            "result": result.result
        }, ensure_ascii=False)

    async def cleanup(self):
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        if self.memory:
            self.memory.end_session()
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
            logger.debug(f"Loaded subagent template: {name}")

    def get_subagent_prompt(self) -> str:
        if not self.templates:
            return "没有可用的子代理"

        lines = ["\n\n## 【SubAgent列表】\n"]
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
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        client=None,
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

        if parent_agent:
            agent.plugin_manager = parent_agent.plugin_manager

        await agent.initialize()

        try:
            result = await agent.run(task)
        finally:
            await agent.cleanup()

        return result
