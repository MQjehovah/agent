import os
import sys
import asyncio
import logging
import signal
from typing import Dict, Any, List, Optional

from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.console import Console
from rich.logging import RichHandler

from llm import LLMClient
from agent import Agent
from scheduler import SchedulerManager
from plugins import PluginManager

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

_shutdown_event: Optional[asyncio.Event] = None


async def interactive_mode(agent: Agent, scheduler: Optional[SchedulerManager] = None):
    global _shutdown_event
    logger.info(f"进入交互模式")

    while _shutdown_event is None or not _shutdown_event.is_set():
        try:
            question = await asyncio.get_event_loop().run_in_executor(
                None, lambda: Prompt.ask(
                    "\n[bold cyan]?[/bold cyan] [cyan]请描述任务[/cyan]")
            )
        except (KeyboardInterrupt, EOFError):
            logger.info("收到中断信号，准备退出...")
            if _shutdown_event:
                _shutdown_event.set()
            break

        if not question.strip():
            continue

        if question.strip().lower() == "/prompt":
            console.print(Panel.fit(
                f"[bold green]系统提示词:[/bold green]\n{agent.system_prompt}",
                border_style="green", box=box.ROUNDED
            ))
            continue

        if question.strip().lower() == "/tools":
            table = Table(title="工具列表", show_header=True,
                          header_style="bold magenta", box=box.ROUNDED)
            table.add_column("名称", style="cyan", no_wrap=True)
            table.add_column("描述", style="green")
            for tool in agent.tool_defs:
                func = tool.get("function", {})
                name = func.get("name", "未知")
                desc = func.get("description", "无描述")
                table.add_row(name, desc)
            console.print(table)
            continue

        if question.strip().lower() == "/skills":
            if agent.skill_manager:
                table = Table(title="技能列表", show_header=True,
                              header_style="bold magenta", box=box.ROUNDED)
                table.add_column("名称", style="cyan")
                for skill_name in agent.skill_manager.list_skills():
                    table.add_row(skill_name)
                console.print(table)
            else:
                console.print("[yellow]无可用技能[/yellow]")
            continue

        if question.strip().lower() == "/sessions":
            if agent.session_manager:
                sessions = agent.session_manager.list_sessions()
                if sessions:
                    table = Table(title=f"会话列表 (共 {len(sessions)} 个)", show_header=True,
                                  header_style="bold magenta", box=box.ROUNDED)
                    table.add_column("Session ID", style="cyan")
                    table.add_column("消息数", style="green", justify="right")
                    for session_id in sessions:
                        session = await agent.session_manager.get_session(session_id)
                        msg_count = len(session.messages) if session else 0
                        table.add_row(session_id, str(msg_count))
                    console.print(table)
                else:
                    console.print("[yellow]暂无会话[/yellow]")
            else:
                console.print("[yellow]Session Manager 未初始化[/yellow]")
            continue

        if question.strip().lower().startswith("/session "):
            target_id = question.strip()[9:].strip()
            if agent.session_manager:
                session = await agent.session_manager.get_session(target_id)
                if session:
                    table = Table(title=f"会话 {target_id} (共 {len(session.messages)} 条消息)",
                                  show_header=True, header_style="bold magenta", box=box.ROUNDED)
                    table.add_column("#", style="dim", width=3)
                    table.add_column("角色", style="cyan", width=10)
                    table.add_column("内容", style="green")
                    for i, msg in enumerate(session.messages, 1):
                        role = str(msg.get("role", "未知"))
                        content = str(msg.get("content", "") or "")
                        table.add_row(str(i), role, content)
                    console.print(table)
                else:
                    console.print(f"[yellow]会话 {target_id} 不存在[/yellow]")
            else:
                console.print("[yellow]Session Manager 未初始化[/yellow]")
            continue
        
        if question.strip().lower().startswith("/messages"):
            session_id = "cli"
            session = None
            if agent.session_manager:
                session = await agent.session_manager.get_session(session_id)
            messages = session.messages if session else []
            table = Table(title=f"当前会话消息 (共 {len(messages)} 条)",
                          show_header=True, header_style="bold magenta", box=box.ROUNDED)
            table.add_column("#", style="dim", width=3)
            table.add_column("角色", style="cyan", width=10)
            table.add_column("内容", style="green")
            for i, msg in enumerate(messages, 1):
                role = str(msg.get("role", "未知"))
                content = str(msg.get("content", "") or "")
                table.add_row(str(i), role, content)
            console.print(table)
            continue

        if question.strip().lower() in ["quit", "exit", "q"]:
            logger.info("再见!")
            break

        console.print()
        result = await agent.run(question, session_id="cli") # 交互模式下的会话

        console.print(Panel.fit(
            f"[bold green]执行结果:[/bold green]\n{result.result}",
            border_style="green", box=box.ROUNDED
        ))


async def main():
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t", help="执行单个任务")
    parser.add_argument("--workspace", "-w",
                        default="config", help="Agent工作目录")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    parser.add_argument("--no-scheduler", action="store_true", help="禁用定时任务")
    parser.add_argument("--no-plugins", action="store_true", help="禁用插件")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    workspace = os.path.abspath(args.workspace)
    src_dir = os.path.dirname(os.path.abspath(__file__))
    client = LLMClient()

    agent = Agent(workspace=workspace, client=client)
    await agent.initialize()

    scheduler = None
    if not args.no_scheduler:
        schedules_path = os.path.join(workspace, "schedules.json")
        scheduler = SchedulerManager(schedules_path)
        scheduler.set_executor(lambda task: agent.run(task))
        scheduler.start()

    async def run_agent(session_id: str, content: str) -> str:
        result = await agent.run(content, session_id=session_id)
        return result.result

    plugin_manager = None
    if not args.no_plugins:
        plugins_dir = os.path.join(src_dir, "plugins")
        plugin_manager = PluginManager(plugins_dir)
        plugin_manager.load_all()
        plugin_manager.register_agent(run_agent)
        plugin_manager.start_all()

    loop = asyncio.get_running_loop()

    def signal_handler():
        logger.info("收到退出信号...")
        if _shutdown_event:
            _shutdown_event.set()

    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    except NotImplementedError:
        if sys.platform == "win32":
            def win_signal_handler(sig, frame):
                logger.info("收到退出信号...")
                if _shutdown_event:
                    _shutdown_event.set()
            signal.signal(signal.SIGINT, win_signal_handler)
            signal.signal(signal.SIGTERM, win_signal_handler)

    try:
        if args.task:
            result = await agent.run(args.task)
            print(result.result)
        else:
            await interactive_mode(agent, scheduler)
        if not _shutdown_event.is_set():
            await _shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("任务被取消")
    finally:
        logger.info("正在清理资源...")

        if plugin_manager:
            plugin_manager.stop_all()
        if scheduler:
            scheduler.stop()
        await agent.cleanup()

        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

        tasks = [t for t in asyncio.all_tasks(
            loop) if t is not asyncio.current_task()]
        if tasks:
            logger.info(f"取消 {len(tasks)} 个后台任务...")
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass

        logger.info("清理完成")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
