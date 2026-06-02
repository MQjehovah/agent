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
from agent_session import AgentSessionManager
from cmd_handler import CommandHandler
from config import Config, validate_config
from llm import LLMClient
from plugins import PluginManager

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

    def handleError(self, record: logging.LogRecord) -> None:
        # Silently swallow BlockingIOError from Rich console
        # (WSL/terminal buffer full — no need to spam stderr)
        import traceback
        try:
            # Check if it's a BlockingIOError before printing anything
            etype, evalue, _ = sys.exc_info()
            if etype is not None and issubclass(etype, (BlockingIOError, OSError)):
                return
        except Exception:
            pass
        # For other errors, fall back to plain stderr
        try:
            msg = self.format(record) + "\n"
            try:
                etype, evalue, etb = sys.exc_info()
                tb = traceback.format_exception(etype, evalue, etb)
                msg += "".join(tb)
            except Exception:
                pass
            os.write(2, msg.encode(sys.stderr.encoding or "utf-8", errors="replace"))
        except Exception:
            pass


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
            result = await agent.run(question, session_id=session_id, user_id="cli:admin", user_name="管理员")
            console.print(Panel.fit(f"[green]任务 #{task_id} 完成:[/green]\n{result.result}",
                                     border_style="green"))
        except asyncio.CancelledError:
            console.print(f"[yellow]任务 #{task_id} 已取消[/yellow]")

        cmd_handler.set_current_task_id(None)
        current_task = None

    _stdin_transport = None

    async def input_reader():
        """后台读取用户输入 — Windows 使用 msvcrt 字符级读取，Unix 用 StreamReader"""
        nonlocal _stdin_transport
        loop = asyncio.get_event_loop()

        if sys.platform == "win32":
            import msvcrt

            async def _readline():
                line = []
                while not shutdown_event.is_set():
                    ch = await loop.run_in_executor(None, msvcrt.getwch)
                    if ch in ("\r", "\n"):
                        sys.stdout.write("\n")
                        break
                    elif ch in ("\x08", "\x7f"):  # backspace
                        if line:
                            removed = line.pop()
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
            _stdin_reader = asyncio.StreamReader()
            _stdin_protocol = asyncio.StreamReaderProtocol(_stdin_reader)
            _stdin_transport, _ = await loop.connect_read_pipe(lambda: _stdin_protocol, sys.stdin)

            async def _readline():
                line = await _stdin_reader.readline()
                return line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")

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
        if _stdin_transport is not None:
            _stdin_transport.close()
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

    from storage import get_storage
    storage = get_storage()

    event_bus = EventBus(storage=storage)
    goal_manager = GoalManager(storage=storage)

    kanban_board = None
    if agent.plugin_manager:
        kp = agent.plugin_manager.get_plugin("kanban")
        if kp:
            kanban_board = kp.get_board()

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
    plugin_manager = agent.plugin_manager

    if plugin_manager:
        dingtalk_plugin = plugin_manager.get_plugin("dingtalk")

        scheduler_plugin = plugin_manager.get_plugin("scheduler")
        if scheduler_plugin:
            async def _schedule_to_perceiver(schedule_task: str):
                await perceiver.handle_schedule({"name": "定时任务", "task": schedule_task})
            scheduler_plugin._agent_executor = _schedule_to_perceiver
            scheduler_plugin.start()

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
        board=kanban_board,
        shutdown_event=shutdown_event,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            pass

    board_info = ""
    if kanban_board:
        stats = kanban_board.get_stats()
        board_info = f"看板: {stats['total']} 个任务 ({stats['by_column']})"

    console.print(
        Panel.fit(
            "[bold green]自主模式已启动[/bold green]\n"
            f"目标数据库: {storage.db_path}\n"
            f"{board_info}\n"
            "信号源: 钉钉消息 | Webhook | 定时任务 | 看板\n"
            "等待事件...",
            border_style="green",
        )
    )

    await auto_loop.run()
    return plugin_manager


async def cleanup(plugin_manager, agent):
    """统一清理资源"""
    try:
        if plugin_manager:
            plugin_manager.stop_all()
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
    parser.add_argument("--workspace", "-w", default="workspace",
                        help="agent工作目录，存放agent产生的文件 (默认: ./workspace)")
    parser.add_argument("--config", "-c", default="config",
                        help="配置目录，包含PROMPT.md、agents/、skills/等 (默认: ./config)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-plugins", action="store_true")
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
    AgentSessionManager.load_config()

    if not args.skip_config_check and not validate_config():
        console.print("[red]配置验证失败[/red]")
        return

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    config_dir = os.path.abspath(args.config)
    workspace = os.path.abspath(args.workspace)
    os.makedirs(workspace, exist_ok=True)
    src_dir = os.path.dirname(os.path.abspath(__file__))

    agent = Agent(workspace=workspace, config_dir=config_dir, client=LLMClient())
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

    web_server = None
    plugin_manager = None

    try:
        start_web = args.web or (args.mode == "autonomous" and not args.no_web)

        if not args.no_plugins:
            plugin_manager = PluginManager(os.path.join(src_dir, "plugins"), config_dir=config_dir)
            plugin_manager.load_all()
            plugin_manager.register_executor(lambda sid, c, uid="", uname="": agent.run(c, session_id=sid, user_id=uid, user_name=uname))
            agent.plugin_manager = plugin_manager

            kanban_plugin = plugin_manager.get_plugin("kanban")
            if kanban_plugin:
                kanban_plugin.set_agent(agent)

            plugin_manager.start_all()

            webhook_plugin = plugin_manager.get_plugin("webhook")
            if webhook_plugin:
                async def _webhook_exec(sid, c, uid="", uname=""):
                    r = await agent.run(c, session_id=sid, user_id=uid, user_name=uname)
                    return r.result if hasattr(r, 'result') else str(r)

                webhook_plugin.agent_executor = _webhook_exec

            scheduler_plugin = plugin_manager.get_plugin("scheduler")
            if scheduler_plugin:
                scheduler_plugin._agent_executor = agent.run
                if not scheduler_plugin._started:
                    scheduler_plugin.start()

        kanban_board = None
        if agent.plugin_manager:
            kp = agent.plugin_manager.get_plugin("kanban")
            if kp:
                kanban_board = kp.get_board()

        if start_web:
            from web import WebServer
            web_server = WebServer(port=args.web_port, loop=asyncio.get_running_loop())
            web_server.set_agent(agent)
            if kanban_board:
                web_server.set_kanban(kanban_board)
            web_server.start()
            console.print(f"[bold green]Web UI:[/bold green] http://localhost:{args.web_port}")

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
        if web_server:
            web_server.stop()
        await cleanup(plugin_manager, agent)
        logger.info("清理完成")


if __name__ == "__main__":
    asyncio.run(main())
