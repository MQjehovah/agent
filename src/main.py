import asyncio
import gc
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

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

# 抑制 MCP SDK 内部日志噪音
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logger = logging.getLogger("agent.main")


async def interactive_mode(agent: Agent, shutdown_event: asyncio.Event):
    """交互模式 - 任务后台执行"""
    session_id = str(uuid.uuid4())
    current_task: asyncio.Task | None = None
    task_counter = 0
    input_queue: asyncio.Queue[str] = asyncio.Queue()
    input_task: asyncio.Task | None = None

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

    async def input_reader():
        """后台读取用户输入 — Windows 使用 msvcrt 字符级读取，Unix 用 StreamReader"""
        loop = asyncio.get_event_loop()

        if sys.platform == "win32":
            import msvcrt

        async def _readline():
            if sys.platform == "win32":
                line = []
                while not shutdown_event.is_set():
                    ch = await loop.run_in_executor(None, msvcrt.getwch)
                    if ch in ("\r", "\n"):
                        sys.stdout.write("\n")
                        break
                    elif ch in ("\x08", "\x7f"):  # backspace
                        if line:
                            removed = line.pop()
                            # CJK 字符占 2 列宽度，退格需要额外清理
                            if '\u1100' <= removed <= '\u9fff' or '\uac00' <= removed <= '\ud7af' or '\uf900' <= removed <= '\uffff' or ord(removed) > 0x20000:
                                sys.stdout.write("\b\b  \b\b")
                            else:
                                sys.stdout.write("\b \b")
                    elif ch == "\x03":
                        raise KeyboardInterrupt
                    elif ch == "\x1a":
                        raise EOFError
                    elif ch.isprintable():
                        line.append(ch)
                        sys.stdout.write(ch)
                    sys.stdout.flush()
                return "".join(line)
            else:
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, sys.stdin)
                question = await reader.readline()
                return question.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")

        while not shutdown_event.is_set():
            try:
                sys.stdout.write("\n? 任务: ")
                sys.stdout.flush()
                question = await _readline()
                if not question:
                    continue
                await input_queue.put(question.strip())
            except (KeyboardInterrupt, EOFError):
                shutdown_event.set()
                break
            except Exception:
                if shutdown_event.is_set():
                    break

    def handle_signal():
        shutdown_event.set()
        if current_task and not current_task.done():
            current_task.cancel()
        if input_task and not input_task.done():
            input_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    input_task = asyncio.create_task(input_reader())

    try:
        while not shutdown_event.is_set():
            try:
                question = await asyncio.wait_for(input_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if not question.strip():
                continue

            if cmd_handler.is_command(question):
                await cmd_handler.handle(question)
                continue

            task_counter += 1
            current_task = asyncio.create_task(run_task(task_counter, question))
            console.print(f"[dim]任务 #{task_counter} 已提交[/dim]")

    finally:
        if input_task and not input_task.done():
            input_task.cancel()
            try:
                await input_task
            except asyncio.CancelledError:
                pass
        if current_task and not current_task.done():
            current_task.cancel()
            try:
                await current_task
            except asyncio.CancelledError:
                pass


async def autonomous_mode(agent: Agent, shutdown_event: asyncio.Event, args, panel=None):
    """自主模式 - 感知-规划-执行-校验循环"""
    from autonomous.eventbus import EventBus
    from autonomous.executor import Executor
    from autonomous.goal import GoalManager
    from autonomous.loop import AutonomousLoop
    from autonomous.panel import TaskPanel
    from autonomous.perceiver import Perceiver
    from autonomous.planner import Planner
    from autonomous.reporter import DingTalkReporter, Reporter
    from autonomous.verifier import Verifier

    workspace = agent.workspace
    db_path = os.path.join(workspace, "autonomous.db")
    panel_path = os.path.join(workspace, "task_panel.json")

    event_bus = EventBus(db_path=db_path)
    goal_manager = GoalManager(db_path)
    panel = TaskPanel(panel_path) if panel is None else panel

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
        panel=panel,
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
            f"任务面板: {panel_path} ({panel.get_stats()['total']} 个任务)\n"
            "信号源: 钉钉消息 | Webhook | 定时任务 | 任务面板\n"
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
        "--web",
        action="store_true",
        help="启动Web UI前端",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web UI端口 (默认8080)",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="禁用Web UI前端",
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

    # 任务面板（Web UI 和自主模式共用）
    from autonomous.panel import TaskPanel
    panel = TaskPanel(os.path.join(workspace, "task_panel.json"))

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

    web_server = None
    scheduler = None
    plugin_manager = None

    try:
        # 启动 Web UI（默认在 autonomous 模式或指定 --web 时启动）
        start_web = args.web or (args.mode == "autonomous" and not args.no_web)

        if start_web:
            from web import WebServer
            web_server = WebServer(port=args.web_port, loop=asyncio.get_running_loop())
            web_server.set_agent(agent)
            web_server.set_panel(panel)
            web_server.start()
            console.print(f"[bold green]Web UI:[/bold green] http://localhost:{args.web_port}")

        if args.mode == "autonomous":
            scheduler, plugin_manager = await autonomous_mode(agent, shutdown_event, args, panel=panel)
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

            await interactive_mode(agent, shutdown_event)
    except asyncio.CancelledError:
        logger.info("任务取消")
    except Exception as e:
        logger.error(f"程序异常退出: {e}", exc_info=True)
    finally:
        logger.info("清理资源...")
        if web_server:
            web_server.stop()
        await cleanup(plugin_manager, scheduler, agent)
        logger.info("清理完成")


if __name__ == "__main__":
    asyncio.run(main())
