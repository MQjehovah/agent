import logging
import uuid
import asyncio
from typing import Optional, Dict, Any, List, TYPE_CHECKING, cast, Sequence
from dataclasses import dataclass, field
from datetime import datetime
import os
import re
import json
from openai.types.chat import ChatCompletionMessageParam

if TYPE_CHECKING:
    from plugins import PluginManager

logger = logging.getLogger("agent.agent")


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
        self.storage = None
        self.plugin_manager: Optional["PluginManager"] = None
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
        from storage import Storage
        self.session_manager = AgentSessionManager()
        self.storage = Storage(self.workspace)
        self.storage.register_agent(self.agent_id, self.name, self.description)

        self._init_subagents()
        self._init_memory()

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

    def _init_tools(self):
        from tools import ToolRegistry, TodoTool, FileTool, SubagentTool, MemoryTool, ShellTool

        self.tool_registry = ToolRegistry()
        self.tool_registry.register_tool(TodoTool())
        self.tool_registry.register_tool(FileTool())
        self.tool_registry.register_tool(SubagentTool())
        self.tool_registry.register_tool(MemoryTool())
        self.tool_registry.register_tool(ShellTool())

        logger.info(
            f"Agent [{self.name}] 已注册 {len(self.tool_registry.list_tools())} 个工具: {[self.tool_registry.list_tools()]}")

    def _init_skills(self):
        skills_dir = os.path.join(self.workspace, "skills")
        if os.path.exists(skills_dir):
            from skills import SkillManager
            self.skill_manager = SkillManager(skills_dir)
            self.system_prompt = self.system_prompt + \
                self.skill_manager.get_skills_prompt()
            logger.info(
                f"Agent [{self.name}] 已加载 {len(self.skill_manager.list_skills())} 个技能: {[self.skill_manager.list_skills()]}")

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
                    logger.debug(
                        f"跳过已禁用的 MCP server: {config.get('name', 'unnamed')}")

            connected = [c.get("name", "unnamed")
                         for c in self.mcp_configs if c.get("enabled", True)]
            logger.info(
                f"Agent [{self.name}] 已连接 {len(connected)} MCP servers: {connected}")

    def _init_subagents(self):
        agents_dir = os.path.join(self.workspace, "agents")
        if os.path.exists(agents_dir):
            self.subagent_manager = SubagentManager(agents_dir)
            self.system_prompt = self.system_prompt + \
                self.subagent_manager.get_subagent_prompt()
            logger.info(
                f"Agent [{self.name}] 已加载 {len(self.subagent_manager.list_templates())} 个子代理: {self.subagent_manager.list_templates()}")

    def _init_memory(self):
        from memory import MemoryManager
        self.memory = MemoryManager(self.workspace, self.storage, self.client)

        memory_context = self.memory.load_memory("")
        if memory_context:
            self.system_prompt += f"\n\n## 【记忆上下文】\n{memory_context}"

        memory_tool = self.tool_registry.get_tool("memory")
        if memory_tool and hasattr(memory_tool, 'set_memory_manager'):
            memory_tool.set_memory_manager(self.memory)
        
        self.memory.start_daily_task()

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
        
        if self.session_manager:
            if session_id:
                session = await self.session_manager.get_session(session_id)
                if session:
                    session.add_message("user", task)
                    if self.storage:
                        self.storage.save_message(session_id, "user", task)
                    logger.info(
                        f"Agent [{self.name}] 复用session: {session_id}, 消息数: {len(session.messages)}")
                else:
                    session = await self.session_manager.create_session(
                        session_id=session_id,
                        system_prompt=self.system_prompt
                    )
                    if self.storage:
                        self.storage.create_session(session_id, self.agent_id)
                        messages = self.storage.get_messages(session_id)
                        if messages:
                            session.messages = cast(List[ChatCompletionMessageParam], messages)
                            logger.info(
                                f"Agent [{self.name}] 从存储恢复session: {session_id}, 消息数: {len(session.messages)}")
                    session.add_message("user", task)
                    if self.storage:
                        self.storage.save_message(session_id, "user", task)
                    logger.debug(f"Agent [{self.name}] 创建新session: {session_id}")
            else:
                session = await self.session_manager.create_session(
                    system_prompt=self.system_prompt
                )
                session_id = session.session_id
                if self.storage:
                    self.storage.create_session(session_id, self.agent_id)
                if self.memory:
                    memory_context = self.memory.load_memory(task)
                    if memory_context:
                        session.messages.insert(0, {
                            "role": "system",
                            "content": f"## 【记忆上下文】\n{memory_context}"
                        })
                session.add_message("user", task)
                if self.storage:
                    self.storage.save_message(session_id, "user", task)
                logger.info(f"Agent [{self.name}] 创建随机session: {session_id}")

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
            f"Agent [{self.name}] [{session.session_id}] 任务开始: {task}...")

        try:
            for i in range(self.max_iterations):
                logger.debug(
                    f"Agent [{self.name}] [{session.session_id}] iteration {i + 1}")

                response = await self._think(session.messages)

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

                        logger.debug(
                            f"Agent [{self.name}] [{session.session_id}] -> tool: {func_name} args: {func_args}")
                        result = await self._execute_tool(func_name, func_args)
                        logger.debug(
                            f"Agent [{self.name}] [{session.session_id}] <- tool: {func_name} result: {result}")

                        session.messages.append({
                            "role": "tool",
                            "name": func_name,
                            "content": str(result),
                            "tool_call_id": tc.get("id", "")
                        })

                    continue

                if msg.get("content"):
                    self.status = "completed"
                    self.result = msg.get("content")
                    break
            else:
                self.status = "max_iterations"
                self.result = "达到最大迭代次数"
                logger.warning(f"Agent [{self.name}] max iterations reached")

        except Exception as e:
            self.status = "failed"
            logger.error(
                f"Agent [{self.name}] [{session.session_id}] failed: {e}")

        logger.debug(
            f"Agent [{self.name}] [{session.session_id}] 任务完成")

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

    async def _think(self, messages: Sequence[ChatCompletionMessageParam]) -> Dict[str, Any]:
        try:
            response = self.client.chat(
                messages,
                self.tool_defs,
                stream=False
            )

            if not response.choices:
                logger.error(
                    f"Agent [{self.name}] no choices returned from LLM")
                raise Exception("No choices returned")

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
                                func_args = json.dumps(
                                    func_args, ensure_ascii=False)
                            except Exception:
                                func_args = "{}"
                    elif isinstance(func_args, dict):
                        logger.warning(
                            f"Agent [{self.name}] function.arguments is dict, converting to JSON string")
                        func_args = json.dumps(func_args, ensure_ascii=False)
                    else:
                        logger.warning(
                            f"Agent [{self.name}] function.arguments is {type(func_args)}, set to empty object")
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

            if self.mcp and self.mcp.has_tool(name):
                return await self.mcp.call_tool(name, args)

            if self.plugin_manager:
                for plugin in self.plugin_manager.plugins.values():
                    logger.debug(
                        f"检查插件 {plugin.name}, enabled={plugin.enabled}")
                    if plugin.enabled:
                        tool_defs = plugin.get_tool_defs()
                        logger.debug(
                            f"插件 {plugin.name} 工具定义: {[t.get('function', {}).get('name') for t in tool_defs]}")
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

        try:
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
        except Exception as e:
            logger.error(f"Subagent execution error: {e}")
            return json.dumps({"success": False, "error": f"子代理执行错误: {e}"}, ensure_ascii=False)

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
            self.memory.stop_daily_task()
        if self.mcp:
            await self.mcp.close()
        logger.info(f"Agent [{self.name}] cleaned up")


class SubagentManager:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.templates: Dict[str, Dict[str, Any]] = {}
        self._load_all()

    def _extract_frontmatter(self, content: str) -> tuple:
        import yaml
        pattern = r'^---\s*\n(.*?)\n---\s*\n(.*)$'
        match = re.match(pattern, content, re.DOTALL)

        if not match:
            return {}, content

        frontmatter_str = match.group(1)
        body = match.group(2)

        try:
            frontmatter = yaml.safe_load(frontmatter_str) or {}
        except yaml.YAMLError as e:
            logger.error(f"YAML parse error: {e}")
            return {}, content

        return frontmatter, body

    def _load_all(self):
        if not self.base_dir or not os.path.exists(self.base_dir):
            logger.warning(f"Subagent directory not found: {self.base_dir}")
            return

        for dir in os.listdir(self.base_dir):
            agent_dir = os.path.join(self.base_dir, dir)
            if not os.path.isdir(agent_dir):
                continue

            prompt_file = os.path.join(agent_dir, "PROMPT.md")
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding="utf-8") as f:
                    content = f.read()
                frontmatter, body = self._extract_frontmatter(content)
                if frontmatter:
                    name = frontmatter.get("name", dir)
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
        if not self.templates:
            return "没有可用的子代理"

        lines = ["\n\n## 【SubAgent列表】\n"]
        for template_data in self.templates.values():
            lines.append(f"名称：[{template_data['name']}]\n")
            lines.append(f"描述：{template_data['description']}\n")
        lines.append("\n通过subagent工具调用激活\n")
        return "\n".join(lines)

    def list_templates(self) -> List[Dict[str, Any]]:
        return [t['name'] for t in self.templates.values()]

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
                agent_name=agent.name,
                status="failed",
                result=f"子代理执行错误: {e}"
            )
            logger.error(f"子代理执行错误: {e}")
        finally:
            await agent.cleanup()

        return result
