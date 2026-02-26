import os
import sys
import asyncio
import logging
from typing import cast, Optional, List, Dict, Any
from dataclasses import dataclass, field
from openai import OpenAI
from openai.types import chat
from openai.types.responses import ResponseFunctionToolCall
from openai.types.chat import ChatCompletionMessageParam
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import json
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

os.environ["PYTHONIOENCODING"] = "utf-8"

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Prompt
from rich.panel import Panel
from rich.table import Table
from rich import box
from rich.logging import RichHandler

console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=True, show_path=False)]
)

logger = logging.getLogger("agent")


client = OpenAI(
    base_url=os.getenv("OPENAI_BASE_URL", "https://api.minimaxi.com/v1/"),
    api_key=os.getenv(
        "OPENAI_API_KEY", "sk-api-UQHBI6bhRHXfg4iuASL66EadYaQbeetEqsJrSqTa6R_6n4-5ba_64vlWjmGq4lGCwnblwQ1usk6j0ukrN64PPGyYpV66WGCHf5wBVXKvVWxoxIWhs3AqL9M"),
    timeout=60.0
)


class Agent:
    def __init__(self, model: str = "MiniMax-M2.5", max_iterations: int = 20):
        self.model = model
        self.max_iterations = max_iterations
        self.tools = {}
        self.tool_defs = []
        self.messages: List[ChatCompletionMessageParam] = []
        self.system_prompt = ""
    
    def set_system_prompt(self, prompt: str):
        self.system_prompt = prompt
        self.messages = [{"role": "system", "content": prompt}]  # type: ignore
    
    def add_tool(self, name: str, description: str, schema: Dict):
        self.tool_defs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema
            }
        })
    
    def add_message(self, role: str, content: str, **kwargs):
        msg = {"role": role, "content": content or ""}
        if kwargs:
            msg.update(kwargs)
        self.messages.append(msg)  # type: ignore
    
    async def think(self, user_input: str) -> Any:
        self.add_message("user", user_input)
        logger.debug(f"User input: {user_input}")
        
        with Progress(
            SpinnerColumn(style="cyan"),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True
        ) as progress:
            task = progress.add_task("AI 思考中...", total=None)
            response = client.chat.completions.create(
                model=self.model,
                messages=self.messages,  # type: ignore
                tools=self.tool_defs
            )
            progress.update(task, completed=True)
        
        logger.debug(f"AI response: {response.choices[0].message.content}")
        return response
    
    async def execute_tool(self, session, name: str, args: Dict) -> str:
        try:
            result = await session.call_tool(name, args)
            if hasattr(result, 'content') and result.content:
                parts = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        parts.append(item.text)
                    elif isinstance(item, str):
                        parts.append(item)
                return "\n".join(parts)
            return "执行成功"
        except Exception as e:
            return f"执行失败: {e}"
    
    async def run(self, task: str, session) -> str:
        soul_path = os.path.join(os.path.dirname(__file__), "SOUL.md")
        system_prompt = open(soul_path, encoding="utf-8").read() if os.path.exists(soul_path) else ""
        self.set_system_prompt(system_prompt)
        
        logger.info(f"开始执行任务: {task}")
        
        for i in range(self.max_iterations):
            logger.debug(f"Iteration {i + 1}/{self.max_iterations}")
            response = await self.think(task)
            msg = response.choices[0].message
            
            self.add_message("assistant", msg.content or "", 
                           tool_calls=[{
                               "id": tc.id,
                               "function": {
                                   "name": tc.function.name,
                                   "arguments": tc.function.arguments
                               }
                           } for tc in (msg.tool_calls or [])] if msg.tool_calls else None)
            
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    func = tc.function
                    if not func or not func.name:
                        continue
                    
                    try:
                        args = json.loads(func.arguments) if isinstance(func.arguments, str) else func.arguments
                    except:
                        args = {}
                    
                    logger.info(f"→ 调用工具: {func.name}")
                    logger.debug(f"Tool arguments: {args}")
                    result = await self.execute_tool(session, func.name, args)
                    
                    self.add_message("tool", result, tool_call_id=tc.id)
                    
                    logger.info(f"✓ {func.name} 执行完成")
                
                continue
            
            if msg.content and msg.content.strip():
                logger.info("任务完成")
                return msg.content
        
        logger.warning("达到最大迭代次数")
        return "达到最大迭代次数"


class AgentApp:
    def __init__(self):
        self.agent = Agent()
        self.session: Optional[ClientSession] = None
        self.scheduler: Optional[SchedulerManager] = None
    
    async def init_tools(self, session: ClientSession):
        mcp_tools = await session.list_tools()
        
        for t in mcp_tools.tools:
            self.agent.add_tool(
                name=t.name,
                description=t.description or "",
                schema=t.inputSchema
            )
        
        logger.info(f"✓ 已加载 {len(mcp_tools.tools)} 个工具: {[t.name for t in mcp_tools.tools]}")
        logger.debug(f"工具详情: {[(t.name, t.description) for t in mcp_tools.tools]}")
        return session
    
    def init_scheduler(self):
        self.scheduler = SchedulerManager(self, self.session)
        self.scheduler.start()
    
    async def run_task(self, task: str, session):
        return await self.agent.run(task, session)
    
    def interactive_mode(self):
        logger.info("进入交互模式")
        
        async def loop():
            while True:
                try:
                    question = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: Prompt.ask("\n[bold cyan]?[/bold cyan] [cyan]请描述任务[/cyan]")
                    )
                except:
                    break
                
                if not question.strip():
                    continue
                
                if question.strip().lower() in ["quit", "exit", "q"]:
                    logger.info("ℹ 再见!")
                    break
                
                console.print()
                result = await self.run_task(question, self.session)
                
                console.print(Panel.fit(
                    f"[bold green]执行结果:[/bold green]\n{result}",
                    border_style="green", box=box.ROUNDED
                ))
        
        return loop()


class SchedulerManager:
    def __init__(self, app: AgentApp, session):
        self.app = app
        self.session = session
        self.scheduler = AsyncIOScheduler()
    
    def load_schedules(self):
        schedules_path = os.path.join(os.path.dirname(__file__), "schedules.json")
        if not os.path.exists(schedules_path):
            logger.warning(f"未找到配置文件: {schedules_path}")
            return []
        
        with open(schedules_path, encoding="utf-8") as f:
            schedules = json.load(f)
        
        enabled_schedules = [s for s in schedules if s.get("enabled", True)]
        logger.info(f"已加载 {len(enabled_schedules)} 个定时任务")
        return enabled_schedules
    
    async def run_scheduled_task(self, schedule: Dict):
        name = schedule.get("name", "未命名任务")
        task = schedule.get("task", "")
        
        logger.info(f"⏰ 触发定时任务: {name}")
        logger.info(f"   任务内容: {task}")
        
        try:
            result = await self.app.run_task(task, self.session)
            logger.info(f"✓ 定时任务完成: {name}")
            logger.debug(f"结果: {result}")
        except Exception as e:
            logger.error(f"✗ 定时任务失败: {name}, 错误: {e}")
    
    def start(self):
        schedules = self.load_schedules()
        
        for schedule in schedules:
            name = schedule.get("name", "未命名")
            cron = schedule.get("cron", "")
            
            try:
                trigger = CronTrigger.from_crontab(cron)
                self.scheduler.add_job(
                    self.run_scheduled_task,
                    trigger=trigger,
                    args=[schedule],
                    name=name
                )
                logger.info(f"✓ 已注册定时任务: {name} ({cron})")
            except Exception as e:
                logger.error(f"✗ 注册定时任务失败: {name}, 错误: {e}")
        
        if self.scheduler.get_jobs():
            self.scheduler.start()
            logger.info(f"定时任务调度器已启动，共 {len(self.scheduler.get_jobs())} 个任务")
            logger.info(f"下次执行时间: {[job.next_run_time for job in self.scheduler.get_jobs()]}")
        else:
            logger.warning("没有可执行的定时任务")


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t", help="执行单个任务")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)
    
    logger.info("启动 Agent 应用")
    
    server_params = StdioServerParameters(
        command="python",
        args=["mcp_server.py"],
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            app = AgentApp()
            await app.init_tools(session)
            app.session = session
            app.init_scheduler()
            
            if args.task:
                result = await app.run_task(args.task, session)
                console.print(Panel.fit(
                    f"[bold green]结果:[/bold green]\n{result}",
                    border_style="green", box=box.ROUNDED
                ))
                return
            
            console.print(Panel.fit(
                "[bold cyan]数据库智能 Agent[/bold cyan]\n"
                "[dim]输入任务描述，自动完成工作[/dim]\n"
                "[dim]输入 [bold]quit[/bold] 退出[/dim]",
                border_style="cyan", box=box.DOUBLE
            ))
            await app.interactive_mode()


if __name__ == "__main__":
    asyncio.run(main())
