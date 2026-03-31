import os
import uuid
import asyncio
import logging
import signal
from typing import Optional
from pathlib import Path

from rich.panel import Panel
from rich.prompt import Prompt
from rich.console import Console
from rich.logging import RichHandler

from dotenv import load_dotenv

# 加载环境配置
_project_root = Path(__file__).parent.parent
_env_file = _project_root / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    _env_example = _project_root / ".env.example"
    if _env_example.exists():
        load_dotenv(_env_example)

os.environ["PYTHONIOENCODING"] = "utf-8"

from llm import LLMClient
from agent import Agent
from scheduler import SchedulerManager
from plugins import PluginManager
from config import validate_config, Config
from cmd_handler import CommandHandler

console = Console()


class AlignedRichHandler(RichHandler):
    def __init__(self, name_width=20, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name_width = name_width

    def emit(self, record: logging.LogRecord) -> None:
        original_name = record.name
        if self.name_width:
            record.name = original_name.ljust(self.name_width)
        try:
            super().emit(record)
        finally:
            record.name = original_name


logging.basicConfig(
    level=logging.INFO,
    format="[%(name)s] - %(message)s",
    datefmt="[%X]",
    handlers=[AlignedRichHandler(name_width=25, console=console, rich_tracebacks=True,
                                 show_time=True, show_path=False)]
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logger = logging.getLogger("agent.main")


async def interactive_mode(agent: Agent, shutdown_event: asyncio.Event):
    """交互模式 - 任务后台执行"""
    session_id = str(uuid.uuid4())
    current_task: Optional[asyncio.Task] = None
    task_counter = 0

    cmd_handler = CommandHandler(agent, session_id, shutdown_event)

    async def run_task(task_id: int, question: str):
        """执行单个任务"""
        nonlocal current_task
        console.print(f"[dim cyan]▶ 任务 #{task_id}[/dim cyan]")
        cmd_handler.set_current_task_id(task_id)

        try:
            result = await agent.run(question, session_id=session_id)
            console.print(Panel.fit(f"[green]任务 #{task_id} 完成:[/green]\n{result.result}",
                                     border_style="green"))
        except asyncio.CancelledError:
            console.print(f"[yellow]任务 #{task_id} 已取消[/yellow]")

        cmd_handler.set_current_task_id(None)
        current_task = None

    try:
        while not shutdown_event.is_set():
            try:
                question = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: Prompt.ask("\n[bold cyan]?[/bold cyan] [cyan]任务[/cyan]")
                )
            except (KeyboardInterrupt, EOFError):
                break

            if not question.strip():
                continue

            if cmd_handler.is_command(question):
                await cmd_handler.handle(question)
                continue

            task_counter += 1
            current_task = asyncio.create_task(run_task(task_counter, question))
            console.print(f"[dim]任务 #{task_counter} 已提交[/dim]")

    finally:
        if current_task:
            current_task.cancel()


async def cleanup(plugin_manager, scheduler, agent):
    """统一清理资源"""
    if plugin_manager:
        plugin_manager.stop_all()
    if scheduler:
        scheduler.stop()
    await agent.cleanup()

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    shutdown_event = asyncio.Event()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", "-w", default="workspace")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-plugins", action="store_true")
    parser.add_argument("--no-scheduler", action="store_true")
    parser.add_argument("--skip-config-check", action="store_true")
    args = parser.parse_args()

    Config.load_from_env()

    if not args.skip_config_check and not validate_config():
        console.print("[red]配置验证失败[/red]")
        return

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    workspace = os.path.abspath(args.workspace)
    src_dir = os.path.dirname(os.path.abspath(__file__))

    agent = Agent(workspace=workspace, client=LLMClient())
    await agent.initialize()

    scheduler = None
    if not args.no_scheduler:
        scheduler = SchedulerManager(os.path.join(workspace, "schedules.json"))
        scheduler.set_executor(lambda t: agent.run(t))
        scheduler.start()

    plugin_manager = None
    if not args.no_plugins:
        plugin_manager = PluginManager(os.path.join(src_dir, "plugins"))
        plugin_manager.load_all()
        plugin_manager.register_executor(lambda sid, c: agent.run(c, session_id=sid))
        plugin_manager.start_all()
        agent.plugin_manager = plugin_manager

    # 信号处理
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            pass

    try:
        await interactive_mode(agent, shutdown_event)
    except asyncio.CancelledError:
        logger.info("任务取消")
    finally:
        logger.info("清理资源...")
        await cleanup(plugin_manager, scheduler, agent)
        logger.info("清理完成")


if __name__ == "__main__":
    asyncio.run(main())