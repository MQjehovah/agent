from rich.logging import RichHandler
from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.console import Console
import os
import sys
import asyncio
import logging
from typing import cast, Optional, List, Dict, Any, Union
from dataclasses import dataclass, field
from openai import OpenAI
import json
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from dingtalk.plugin import DingTalkPlugin
from agent_session import AgentSessionManager
from skills import SkillLoader, SkillResult, SkillManager
from tools import ToolRegistry, TodoTool, FileTool, SubagentTool
from mcps import MCPManager
from subagent import SubagentManager

os.environ["PYTHONIOENCODING"] = "utf-8"


console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True,
                          show_time=True, show_path=False)]
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("agent")


class SchedulerManager:
    def __init__(self, config_path: str = "schedules.json"):
        self.config_path = config_path
        self.scheduler: Optional[AsyncIOScheduler] = None
        self._started = False
        self._task_executor = None

    def set_executor(self, executor):
        self._task_executor = executor

    def load_schedules(self):
        schedules_path = os.path.join(
            os.path.dirname(__file__), self.config_path)
        if not os.path.exists(schedules_path):
            logger.warning(f"未找到配置文件: {schedules_path}")
            return []

        with open(schedules_path, encoding="utf-8") as f:
            schedules = json.load(f)

        enabled_schedules = [s for s in schedules if s.get("enabled", True)]
        logger.info(f"已加载 {len(enabled_schedules)} 个定时任务")
        return enabled_schedules

    async def _execute_task(self, schedule: Dict):
        name = schedule.get("name", "未命名任务")
        task = schedule.get("task", "")

        logger.info(f"⏰ 触发定时任务: {name}")
        logger.info(f"   任务内容: {task}")

        if not self._task_executor:
            logger.error("未设置任务执行器")
            return

        try:
            result = await self._task_executor(task)
            logger.info(f"✓ 定时任务完成: {name}")
            logger.debug(f"结果: {result}")
        except Exception as e:
            logger.error(f"✗ 定时任务失败: {name}, 错误: {e}")

    def start(self):
        self.scheduler = AsyncIOScheduler()
        schedules = self.load_schedules()

        for schedule in schedules:
            name = schedule.get("name", "未命名")
            cron = schedule.get("cron", "")

            try:
                trigger = CronTrigger.from_crontab(cron)
                self.scheduler.add_job(  # type: ignore
                    self._execute_task,
                    trigger=trigger,
                    args=[schedule],
                    name=name
                )
                logger.info(f"✓ 已注册定时任务: {name} ({cron})")
            except Exception as e:
                logger.error(f"✗ 注册定时任务失败: {name}, 错误: {e}")

        scheduler = self.scheduler
        if scheduler.get_jobs():  # type: ignore
            scheduler.start()  # type: ignore
            self._started = True
            # type: ignore
            logger.info(f"定时任务调度器已启动，共 {len(scheduler.get_jobs())} 个任务")
            # type: ignore
            logger.info(
                f"下次执行时间: {[job.next_run_time for job in scheduler.get_jobs()]}")
        else:
            logger.warning("没有可执行的定时任务")


class LLMClient:
    def __init__(self, model: str = "MiniMax-M2.5", base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.client = OpenAI(
            base_url=base_url or os.getenv(
                "OPENAI_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),

            api_key=api_key or os.getenv(
                "OPENAI_API_KEY", "sk-sp-39ab191a77af4bbda827e309afa60b12"),
            timeout=60.0
        )

    def chat(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], stream: bool = True):
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            stream=stream
        )

    def chat_sync(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]):
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            stream=False
        )


class Agent:
    def __init__(self, client: LLMClient = None):
        self.client = client
        self.system_prompt = ""
        self.tool_registry: ToolRegistry = None
        self.skill_manager: Optional[SkillManager] = None
        self.subagent_manager: Optional[SubagentManager] = None

        self.session_manager: Optional[AgentSessionManager] = AgentSessionManager(
        )

    async def initialize(self):
        self._init_prompt()
        self._init_subagent()
        self._init_tools()
        self._init_skills()
        await self._init_mcp()
        self._init_scheduler()
        # self._init_dingtalk_plugin()
        # self._init_webhook_plugin()

        logger.debug(f"system_prompt:{self.system_prompt}")
        logger.debug(f"system_tools:{self.tool_defs}")

    def _init_prompt(self):
        soul_path = os.path.join(os.path.dirname(
            __file__), "../config", "SOUL.md")
        system_prompt = open(
            soul_path, encoding="utf-8").read() if os.path.exists(soul_path) else ""
        self.system_prompt = system_prompt

    def _init_subagent(self):
        agents_dir = os.path.join(os.path.dirname(
            __file__), "../config", "agents")
        self.subagent_manager = SubagentManager(agents_dir)

        self.system_prompt = self.system_prompt + \
            self.subagent_manager.get_subagent_prompt()

    def _init_tools(self):
        self.tool_registry = ToolRegistry()
        self.tool_registry.register_tool(TodoTool())
        self.tool_registry.register_tool(FileTool())
        self.tool_registry.register_tool(SubagentTool())

    def _init_skills(self):
        skills_dir = os.path.join(os.path.dirname(
            __file__), "../config", "skills")
        self.skill_manager = SkillManager(skills_dir)
        self.system_prompt = self.system_prompt + \
            self.skill_manager.get_skills_prompt()

    async def _init_mcp(self):
        config_path = os.path.join(os.path.dirname(
            __file__), "../config", "mcp_servers.json")
        self.mcp = MCPManager(config_path)
        await self.mcp.connect()

    def _init_scheduler(self):
        schedules_dir = os.path.join(os.path.dirname(
            __file__), "../config", "schedules.json")
        self.scheduler = SchedulerManager(schedules_dir)
        self.scheduler.set_executor(self.run)
        self.scheduler.start()

    def _init_dingtalk_plugin(self):
        try:
            self.dingtalk_plugin = DingTalkPlugin()
            self.dingtalk_plugin.register_agent(self.run_with_session_id)
            self.dingtalk_plugin.start()
            logger.info("钉钉插件服务已启动")
        except Exception as e:
            logger.warning(f"钉钉插件启动失败: {e}")

    def _init_webhook_plugin(self):
        try:
            from webhook import WebhookPlugin
            self.webhook_plugin = WebhookPlugin()
            self.webhook_plugin.register_agent(self.run_with_session_id)
            self.webhook_plugin.start()
            logger.info("Webhook插件服务已启动")
        except Exception as e:
            logger.warning(f"Webhook插件启动失败: {e}")

    @property
    def tool_defs(self):
        tools = []
        tools.extend(self.tool_registry.get_tool_definitions())
        if hasattr(self, 'mcp') and self.mcp:
            tools.extend(self.mcp.tool_defs)
        if self.skill_manager:
            tools.extend(self.skill_manager.get_tool_definitions())
        return tools

    async def think(self, session) -> Any:
        # logger.debug(f"调用API: {session.messages} \n {self.tool_defs}")
        response = self.client.chat(
            session.messages, self.tool_defs, stream=True)

        content_chunks = []
        tool_calls_data = {}

        for chunk in response:
            delta = chunk.choices[0].delta

            if delta.content:
                content_chunks.append(delta.content)
                # print(delta.content, end="", flush=True)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_data:
                        tool_calls_data[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""}
                        }
                    if tc.id:
                        tool_calls_data[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_data[idx]["function"]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_data[idx]["function"]["arguments"] += tc.function.arguments

        # print()

        full_content = "".join(content_chunks)
        tool_calls_list = list(tool_calls_data.values()
                               ) if tool_calls_data else None

        class MockMessage:
            def __init__(self, content, tool_calls):
                self.content = content
                self.tool_calls = tool_calls

        class MockResponse:
            def __init__(self, message):
                self.choices = [type('Choice', (), {'message': message})]

        return MockResponse(MockMessage(full_content, tool_calls_list))

    async def execute_tool(self, name: str, args: Dict) -> str:
        if name == "subagent":
            return await self._execute_subagent(args)
        if self.tool_registry.has_tool(name):
            return await self.tool_registry.execute(name, args)
        if self.skill_manager and name == "execute_skill":
            return await self.skill_manager.execute_tool(name, args)
        return await self.mcp.call_tool(name, args)

    async def _execute_subagent(self, args: Dict) -> str:
        from subagent import Subagent
        import json
        
        task = args.get("task")
        if not task:
            return json.dumps({"success": False, "error": "缺少task参数"}, ensure_ascii=False)
        
        config = self.subagent_manager.create_config(
            name=args.get("name", ""),
            system_prompt=args.get("system_prompt", ""),
            tools=args.get("tools"),
            max_iterations=args.get("max_iterations", 50),
            template=args.get("template", "")
        )
        
        subagent = Subagent(
            task=task,
            config=config,
            client=self.client,
            tool_registry=self.tool_registry,
            mcp_manager=self.mcp if hasattr(self, 'mcp') else None,
            skill_manager=self.skill_manager
        )
        
        result = await subagent.run()
        
        return json.dumps({
            "success": result.status == "completed",
            "subagent_id": result.subagent_id,
            "name": config.name,
            "status": result.status,
            "result": result.result,
            "iterations": result.iterations,
            "error": result.error
        }, ensure_ascii=False)

    def list_skills(self) -> List[Dict[str, Any]]:
        if not self.skill_manager:
            return []
        return self.skill_manager.list_skills()

    async def connect_mcp(self, config: Dict[str, Any]) -> bool:
        """动态连接MCP服务器

        Args:
            config: MCP服务器配置，包含name、command、args等

        Returns:
            是否连接成功
        """
        return await self.mcp.connect_server(config)

    async def disconnect_mcp(self, name: str) -> bool:
        """断开MCP服务器

        Args:
            name: 服务器名称

        Returns:
            是否断开成功
        """
        return await self.mcp.disconnect_server(name)

    async def reload_mcp(self, name: str = None) -> Dict[str, bool]:
        """重载MCP服务器

        Args:
            name: 服务器名称，为None时重载全部

        Returns:
            各服务器的重载结果
        """
        if name:
            success = await self.mcp.reload_server(name)
            return {name: success}
        return await self.mcp.reload_all()

    def list_tools(self) -> Dict[str, List[str]]:
        """列出所有工具

        Returns:
            按类型分组的工具列表
        """
        return {
            "builtin": self.tool_registry.list_tools(),
            "mcp": [t["function"]["name"] for t in self.mcp.tool_defs] if hasattr(self, 'mcp') and self.mcp else [],
            "skills": [t["function"]["name"] for t in self.skill_manager.get_tool_definitions()] if self.skill_manager else []
        }

    async def execute_skill(
        self,
        skill_name: str,
        user_input: str,
        variables: Dict[str, Any] = None
    ) -> SkillResult:
        if not self.skill_manager:
            return SkillResult(success=False, error="技能管理器未初始化")

        result = await self.skill_manager.execute_tool("execute_skill", {
            "skill_name": skill_name,
            "user_input": user_input
        })

        return SkillResult(
            success=True,
            data=result,
            metadata={"skill_name": skill_name}
        )

    async def run(self, task: str, session=None) -> str:
        if not session:
            session = await self.session_manager.create_session(system_prompt=self.system_prompt)
        session.add_message("user", task)

        logger.info(f"开始执行任务: {task}")
        for i in range(session.max_iterations):
            logger.debug(f"Iteration {i + 1}/{session.max_iterations}")
            response = await self.think(session)
            msg = response.choices[0].message
            session.add_message("assistant", msg.content or "",
                                tool_calls=msg.tool_calls if msg.tool_calls else None)

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    if isinstance(tc, dict):
                        tc_id = tc.get("id", "")
                        func_data = tc.get("function", {})
                        func_name = func_data.get("name", "")
                        func_args = func_data.get("arguments", "")
                    else:
                        tc_id = tc.id
                        func = tc.function
                        func_name = func.name if func else ""
                        func_args = func.arguments if func else ""

                    if not func_name:
                        continue

                    try:
                        args = json.loads(func_args) if isinstance(
                            func_args, str) else func_args
                    except:
                        args = {}

                    logger.info(f"→ 调用工具: {func_name}")
                    logger.debug(f"Tool arguments: {args}")
                    result = await self.execute_tool(func_name, args)
                    logger.debug(f"Tool results: {result}")
                    logger.info(f"✓ {func_name} 执行完成")

                    session.add_message(
                        "tool", result, tool_call_id=tc_id)

                continue

            if msg.content and msg.content.strip():
                logger.info("任务完成")
                return msg.content

        logger.warning("达到最大迭代次数")
        return "达到最大迭代次数"

    async def run_with_session_id(
        self,
        session_id: str,
        task: str,
        system_prompt: str = ""
    ) -> str:
        session = await self.session_manager.get_session(session_id)
        if not session:
            session = await self.session_manager.create_session(
                session_id=session_id,
                system_prompt=system_prompt or self.system_prompt
            )
        return await self.run(task, session)

    async def interactive_mode(self):
        logger.info("进入交互模式")
        agent_session = await self.session_manager.create_session(system_prompt=self.system_prompt)
        while True:
            try:
                question = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: Prompt.ask(
                        "\n[bold cyan]?[/bold cyan] [cyan]请描述任务[/cyan]")
                )
            except:
                break

            if not question.strip():
                continue

            if question.strip().lower() in ["quit", "exit", "q"]:
                logger.info("ℹ 再见!")
                break

            console.print()
            result = await self.run(question, agent_session)

            console.print(Panel.fit(
                f"[bold green]执行结果:[/bold green]\n{result}",
                border_style="green", box=box.ROUNDED
            ))


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t", help="执行单个任务")
    parser.add_argument("--skill", "-s", help="执行指定技能")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    logger.info("启动 Agent")
    agent = Agent(LLMClient())
    await agent.initialize()

    if args.skill:
        result = await agent.execute_skill(args.skill, args.task or "")
        console.print(Panel.fit(
            f"[bold green]技能执行结果:[/bold green]\n{result.data if result.success else result.error}",
            border_style="green" if result.success else "red",
            box=box.ROUNDED
        ))
        return

    if args.task:
        result = await agent.run(args.task)
        console.print(Panel.fit(
            f"[bold green]结果:[/bold green]\n{result}",
            border_style="green", box=box.ROUNDED
        ))
        return

    console.print(Panel.fit(
        "[bold cyan]Agent[/bold cyan]\n"
        "[dim]输入任务描述，自动完成工作[/dim]\n"
        "[dim]输入 [bold]quit[/bold] 退出[/dim]",
        border_style="cyan", box=box.DOUBLE
    ))

    await agent.interactive_mode()


if __name__ == "__main__":
    asyncio.run(main())
