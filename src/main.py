import asyncio
import logging
import os
import signal
import uuid
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.prompt import Prompt

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

from agent import Agent
from cmd_handler import CommandHandler
from config import Config, validate_config
from llm import LLMClient
from plugins import PluginManager
from scheduler import SchedulerManager

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
    current_task: asyncio.Task | None = None
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
    from autonomous.executor import Executor
    from autonomous.goal import GoalManager
    from autonomous.loop import AutonomousLoop
    from autonomous.perceiver import Perceiver
    from autonomous.planner import Planner
    from autonomous.reporter import DingTalkReporter, Reporter
    from autonomous.verifier import Verifier

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
    if not args.no_plugins:
        plugin_manager = PluginManager(os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins"))
        plugin_manager.load_all()
        agent.plugin_manager = plugin_manager

        async def _plugin_to_perceiver(session_id: str, content: str):
            await perceiver.handle_dingtalk_message({
                "text": content,
                "session_id": session_id,
                "sender_nick": "用户",
            })
            return "已收到，正在处理中..."

        plugin_manager.register_executor(_plugin_to_perceiver)
        plugin_manager.start_all()

        dingtalk_plugin = plugin_manager.get_plugin("dingtalk")
    else:
        plugin_manager = None

    if (
        dingtalk_plugin
        and hasattr(dingtalk_plugin, "sessions")
        and dingtalk_plugin.sessions
    ):
        reporter = DingTalkReporter(dingtalk_plugin=dingtalk_plugin)
    else:
        reporter = Reporter()

    scheduler = None
    if not args.no_scheduler:
        scheduler = SchedulerManager(os.path.join(workspace, "schedules.json"))

        async def _schedule_to_perceiver(schedule_task: str):
            await perceiver.handle_schedule({"name": "定时任务", "task": schedule_task})

        scheduler.set_executor(_schedule_to_perceiver)
        scheduler.start()

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

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            pass

    console.print(
        Panel.fit(
            "[bold green]自主模式已启动[/bold green]\n"
            f"目标数据库: {db_path}\n"
            f"自主巡检间隔: {auto_loop._discovery_interval}s\n"
            "信号源: 钉钉消息 | Webhook | 定时任务 | 自主巡检\n"
            "等待事件...",
            border_style="green",
        )
    )

    await auto_loop.run()
    return scheduler, plugin_manager


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

    # 关闭事件循环前执行一次 GC，让 subprocess transport 在循环还活着时被回收
    import gc
    gc.collect()


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
    parser.add_argument(
        "--agent",
        "-a",
        default="",
        help="指定子代理名称执行任务",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="要执行的任务内容",
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

    if args.agent:
        agent_name = args.agent
        task = " ".join(args.task) if args.task else ""
        if not task:
            task = input("请输入任务内容: ")

        console.print(f"\n[bold cyan]子代理模式[/bold cyan]: {agent_name}")
        console.print(f"任务: {task}\n")

        result = await agent.run(
            f"请使用 subagent 工具将以下任务交给「{agent_name}」:\n{task}"
        )
        console.print(result.result if hasattr(result, "result") else result)
        return

    scheduler = None
    plugin_manager = None

    try:
        if args.mode == "autonomous":
            scheduler, plugin_manager = await autonomous_mode(agent, shutdown_event, args)
        else:
            if not args.no_scheduler:
                scheduler = SchedulerManager(os.path.join(workspace, "schedules.json"))
                scheduler.set_executor(lambda t: agent.run(t))
                scheduler.start()

            if not args.no_plugins:
                plugin_manager = PluginManager(os.path.join(src_dir, "plugins"))
                plugin_manager.load_all()
                plugin_manager.register_executor(lambda sid, c: agent.run(c, session_id=sid))
                plugin_manager.start_all()
                agent.plugin_manager = plugin_manager

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)
                except NotImplementedError:
                    pass

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
