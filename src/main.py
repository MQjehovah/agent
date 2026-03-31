import os
import uuid
import asyncio
import logging
import signal
import time
import threading
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


class ESCListener:
    """ESC键监听器 - 双ESC取消当前任务"""

    def __init__(self, cancel_event: asyncio.Event, timeout: float = 1.0):
        self.cancel_event = cancel_event
        self.timeout = timeout
        self._last_esc_time = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)

    def _listen_loop(self):
        try:
            import msvcrt
            while self._running:
                if msvcrt.kbhit() and msvcrt.getch() == b'\x1b':
                    now = time.time()
                    if now - self._last_esc_time < self.timeout:
                        logger.info("双ESC，取消任务")
                        asyncio.get_event_loop().call_soon_threadsafe(self.cancel_event.set)
                        self._last_esc_time = 0
                    else:
                        self._last_esc_time = now
                        console.print("[dim yellow]再按ESC取消[/dim yellow]")
                time.sleep(0.05)
        except ImportError:
            import select
            import sys
            while self._running:
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    if sys.stdin.read(1) == '\x1b':
                        now = time.time()
                        if now - self._last_esc_time < self.timeout:
                            asyncio.get_event_loop().call_soon_threadsafe(self.cancel_event.set)
                            self._last_esc_time = 0
                        else:
                            self._last_esc_time = now
                            console.print("[dim yellow]再按ESC取消[/dim yellow]")
                time.sleep(0.05)


async def interactive_mode(agent: Agent, shutdown_event: asyncio.Event):
    """交互模式 - 任务后台执行，支持双ESC取消"""
    session_id = str(uuid.uuid4())
    cancel_event = asyncio.Event()
    esc_listener = ESCListener(cancel_event)
    esc_listener.start()

    task_queue: asyncio.Queue = asyncio.Queue()
    current_task: Optional[asyncio.Task] = None
    task_counter = 0

    cmd_handler = CommandHandler(agent, session_id, task_queue)
    cmd_handler.set_cancel_event(cancel_event)

    async def run_task(task_id: int, question: str):
        """执行单个任务"""
        nonlocal current_task
        console.print(f"[dim cyan]▶ 任务 #{task_id}[/dim cyan]")
        cancel_event.clear()

        current_task = asyncio.create_task(agent.run(question, session_id=session_id))
        cancel_wait = asyncio.create_task(cancel_event.wait())

        done, _ = await asyncio.wait([current_task, cancel_wait], return_when=asyncio.FIRST_COMPLETED)

        if cancel_wait in done:
            current_task.cancel()
            try:
                await current_task
            except asyncio.CancelledError:
                console.print(f"[yellow]任务 #{task_id} 已取消[/yellow]")
        else:
            cancel_wait.cancel()
            result = current_task.result()
            console.print(Panel.fit(f"[green]任务 #{task_id} 完成:[/green]\n{result.result}",
                                     border_style="green", box=box.ROUNDED))
        current_task = None

    async def executor():
        """后台任务执行器"""
        while True:
            try:
                item = await asyncio.wait_for(task_queue.get(), timeout=0.5)
                if item is None:
                    break
                cmd_handler.set_current_task_id(item[0])
                await run_task(item[0], item[1])
                cmd_handler.set_current_task_id(None)
                task_queue.task_done()
            except asyncio.TimeoutError:
                continue

    executor_task = asyncio.create_task(executor())

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
                _, continue_loop = await cmd_handler.handle(question)
                if not continue_loop:
                    break
                continue

            task_counter += 1
            await task_queue.put((task_counter, question))
            console.print(f"[dim]任务 #{task_counter} 已入队[/dim]")

    finally:
        esc_listener.stop()
        await task_queue.put(None)
        if current_task:
            current_task.cancel()
        await asyncio.wait_for(executor_task, timeout=1.0)


async def cleanup(plugin_manager, scheduler, agent):
    """统一清理资源"""
    if plugin_manager:
        plugin_manager.stop_all()
    if scheduler:
        scheduler.stop()
    await agent.cleanup()

    # 取消所有剩余任务
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main():
    shutdown_event = asyncio.Event()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t", help="执行单个任务")
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
        if args.task:
            result = await agent.run(args.task)
            print(result.result)
        else:
            await interactive_mode(agent, shutdown_event)
    except asyncio.CancelledError:
        logger.info("任务取消")
    finally:
        logger.info("清理资源...")
        await cleanup(plugin_manager, scheduler, agent)
        logger.info("完成")


if __name__ == "__main__":
    asyncio.run(main())