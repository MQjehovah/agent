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

    cmd_handler = CommandHandler(agent, session_id, on_exit=shutdown_event.set)

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


async def autonomous_mode(agent: Agent, shutdown_event: asyncio.Event, args):
    """自主模式 - 感知-规划-执行-校验循环"""
    from autonomous.eventbus import EventBus
    from autonomous.goal import GoalManager
    from autonomous.perceiver import Perceiver
    from autonomous.planner import Planner
    from autonomous.executor import Executor
    from autonomous.verifier import Verifier
    from autonomous.reporter import Reporter, DingTalkReporter
    from autonomous.loop import AutonomousLoop

    workspace = agent.workspace
    db_path = os.path.join(workspace, "autonomous.db")

    event_bus = EventBus()
    goal_manager = GoalManager(db_path)

    tool_summary = ""
    if hasattr(agent, "_get_tool_summary"):
        tool_summary = agent._get_tool_summary()

    subagent_summary = ""
    if agent.subagent_manager:
        subagent_summary = agent.subagent_manager.get_subagent_prompt()

    perceiver = Perceiver(event_bus=event_bus, agent=agent)
    planner = Planner(
        client=agent.client,
        tool_summary=tool_summary,
        subagent_summary=subagent_summary,
    )

    dingtalk_plugin = None
    if agent.plugin_manager:
        dingtalk_plugin = agent.plugin_manager.get_plugin("dingtalk")

    if (
        dingtalk_plugin
        and hasattr(dingtalk_plugin, "sessions")
        and dingtalk_plugin.sessions
    ):
        reporter = DingTalkReporter(dingtalk_plugin=dingtalk_plugin)
    else:
        reporter = Reporter()

    executor = Executor(agent=agent, reporter=reporter)
    verifier = Verifier(client=agent.client)

    auto_loop = AutonomousLoop(
        event_bus=event_bus,
        agent=agent,
        goal_manager=goal_manager,
        planner=planner,
        executor=executor,
        verifier=verifier,
        reporter=reporter,
        perceiver=perceiver,
        shutdown_event=shutdown_event,
    )

    console.print(
        Panel.fit(
            "[bold green]自主模式已启动[/bold green]\n"
            f"目标数据库: {db_path}\n"
            f"自主巡检间隔: {auto_loop._discovery_interval}s\n"
            "等待事件...",
            border_style="green",
        )
    )

    await auto_loop.run()


async def cleanup(plugin_manager, scheduler, agent):
    """统一清理资源"""
    try:
        if plugin_manager:
            plugin_manager.stop_all()
        if scheduler:
            scheduler.stop()
        await agent.cleanup()
    except asyncio.CancelledError:
        logger.warning("清理过程被取消")
    except Exception as e:
        logger.error(f"清理过程出错: {e}")

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
    parser.add_argument(
        "--mode",
        "-m",
        choices=["interactive", "autonomous"],
        default="interactive",
        help="运行模式",
    )
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
        if args.mode == "autonomous":
            await autonomous_mode(agent, shutdown_event, args)
        else:
            await interactive_mode(agent, shutdown_event)
    except asyncio.CancelledError:
        logger.info("任务取消")
    except Exception as e:
        logger.error(f"程序异常退出: {e}", exc_info=True)
    finally:
        logger.info("清理资源...")
        await cleanup(plugin_manager, scheduler, agent)
        logger.info("清理完成")


if __name__ == "__main__":
    asyncio.run(main())