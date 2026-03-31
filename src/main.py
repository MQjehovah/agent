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

# 加载 .env 文件
from dotenv import load_dotenv

# 从项目根目录加载 .env
_project_root = Path(__file__).parent.parent
_env_file = _project_root / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
    print(f"✓ 已加载环境配置: {_env_file}")
else:
    # 尝试加载 .env.example 作为后备
    _env_example = _project_root / ".env.example"
    if _env_example.exists():
        load_dotenv(_env_example)
        print(f"⚠ 使用示例配置: {_env_example}")

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
        # 临时保存原始名称
        original_name = record.name
        if self.name_width:
            # 固定宽度，左对齐，右侧补空格
            record.name = original_name.ljust(self.name_width)
        try:
            super().emit(record)
        finally:
            # 恢复原始名称，避免影响其他处理器
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

_shutdown_event: Optional[asyncio.Event] = None
_cancel_event: Optional[asyncio.Event] = None
_current_task: Optional[asyncio.Task] = None


class ESCListener:
    """ESC键监听器 - 检测双ESC取消当前任务"""

    def __init__(self, cancel_event: asyncio.Event, timeout: float = 1.0):
        self.cancel_event = cancel_event
        self.timeout = timeout
        self._last_esc_time = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """启动监听器"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        logger.debug("ESC监听器已启动")

    def stop(self):
        """停止监听器"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        logger.debug("ESC监听器已停止")

    def _listen_loop(self):
        """监听循环"""
        try:
            import msvcrt  # Windows
            while self._running:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    # ESC键的ASCII码是27
                    if ch == b'\x1b':
                        current_time = time.time()
                        if current_time - self._last_esc_time < self.timeout:
                            # 双ESC检测到，触发取消
                            logger.info("检测到双ESC，取消当前任务")
                            self._last_esc_time = 0
                            # 在事件循环中设置取消事件
                            try:
                                loop = asyncio.get_event_loop()
                                loop.call_soon_threadsafe(self.cancel_event.set)
                            except Exception as e:
                                logger.error(f"设置取消事件失败: {e}")
                        else:
                            self._last_esc_time = current_time
                            console.print("[dim yellow]再按一次ESC取消任务[/dim yellow]")
                time.sleep(0.05)
        except ImportError:
            # 非Windows系统，使用select
            try:
                import select
                import sys
                while self._running:
                    if select.select([sys.stdin], [], [], 0.1)[0]:
                        ch = sys.stdin.read(1)
                        if ch == '\x1b':
                            current_time = time.time()
                            if current_time - self._last_esc_time < self.timeout:
                                logger.info("检测到双ESC，取消当前任务")
                                self._last_esc_time = 0
                                try:
                                    loop = asyncio.get_event_loop()
                                    loop.call_soon_threadsafe(self.cancel_event.set)
                                except Exception as e:
                                    logger.error(f"设置取消事件失败: {e}")
                            else:
                                self._last_esc_time = current_time
                                console.print("[dim yellow]再按一次ESC取消任务[/dim yellow]")
                    time.sleep(0.05)
            except Exception as e:
                logger.warning(f"ESC监听器初始化失败: {e}")


async def interactive_mode(agent: Agent):
    global _shutdown_event, _cancel_event, _current_task
    logger.info(f"进入交互模式")
    session_id = str(uuid.uuid4())

    # 初始化取消事件和ESC监听器
    _cancel_event = asyncio.Event()
    esc_listener = ESCListener(_cancel_event)
    esc_listener.start()

    # 任务队列和执行状态
    task_queue: asyncio.Queue = asyncio.Queue()
    current_task_id: Optional[int] = None
    task_counter = 0
    executor_running = True
    current_agent_task: Optional[asyncio.Task] = None

    # 初始化命令处理器
    cmd_handler = CommandHandler(agent, session_id, task_queue)
    cmd_handler.set_cancel_event(_cancel_event)

    async def task_executor():
        """后台任务执行器"""
        nonlocal current_task_id, executor_running, current_agent_task
        while executor_running:
            try:
                # 从队列获取任务
                task_item = await asyncio.wait_for(task_queue.get(), timeout=0.5)
                if task_item is None:  # 退出信号
                    break

                task_id, question, sess_id = task_item
                current_task_id = task_id
                cmd_handler.set_current_task_id(task_id)
                console.print(f"[dim cyan]▶ 开始执行任务 #{task_id}[/dim cyan]")

                # 重置取消事件
                _cancel_event.clear()

                # 执行任务并监听取消
                try:
                    current_agent_task = asyncio.create_task(agent.run(question, session_id=sess_id))
                    cancel_wait = asyncio.create_task(_cancel_event.wait())

                    done, pending = await asyncio.wait(
                        [current_agent_task, cancel_wait],
                        return_when=asyncio.FIRST_COMPLETED
                    )

                    # 处理取消
                    if cancel_wait in done:
                        current_agent_task.cancel()
                        try:
                            await current_agent_task
                        except asyncio.CancelledError:
                            console.print(f"[yellow]任务 #{task_id} 已取消[/yellow]")
                    else:
                        # 正常完成
                        cancel_wait.cancel()
                        try:
                            await cancel_wait
                        except asyncio.CancelledError:
                            pass
                        result = current_agent_task.result()
                        console.print(Panel.fit(
                            f"[bold green]任务 #{task_id} 完成:[/bold green]\n{result.result}",
                            border_style="green", box=box.ROUNDED
                        ))

                except asyncio.CancelledError:
                    console.print(f"[yellow]任务 #{task_id} 已取消[/yellow]")
                except Exception as e:
                    console.print(Panel.fit(
                        f"[bold red]任务 #{task_id} 失败:[/bold red]\n{e}",
                        border_style="red", box=box.ROUNDED
                    ))

                current_task_id = None
                cmd_handler.set_current_task_id(None)
                current_agent_task = None
                task_queue.task_done()

            except asyncio.TimeoutError:
                continue  # 队列空，继续等待
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"任务执行器错误: {e}")
                current_task_id = None
                current_agent_task = None

    # 启动后台执行器
    executor_task = asyncio.create_task(task_executor())

    try:
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

            # 处理命令
            if cmd_handler.is_command(question):
                handled, should_continue = await cmd_handler.handle(question)
                if not should_continue:
                    logger.info("再见!")
                    esc_listener.stop()
                    executor_running = False
                    await task_queue.put(None)  # 发送退出信号
                    await executor_task
                    break
                continue

            # 将任务加入队列
            task_counter += 1
            await task_queue.put((task_counter, question, session_id))
            console.print(f"[dim cyan]任务 #{task_counter} 已加入队列[/dim cyan]")

    finally:
        esc_listener.stop()
        executor_running = False
        try:
            await task_queue.put(None)
            await asyncio.wait_for(executor_task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            executor_task.cancel()
            try:
                await executor_task
            except asyncio.CancelledError:
                pass


async def main():
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t", help="执行单个任务")
    parser.add_argument("--workspace", "-w",
                        default="workspace", help="Agent工作目录")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    parser.add_argument("--no-scheduler", action="store_true", help="禁用定时任务")
    parser.add_argument("--no-plugins", action="store_true", help="禁用插件")
    parser.add_argument("--skip-config-check", action="store_true", help="跳过配置验证")
    args = parser.parse_args()

    # 加载配置
    Config.load_from_env()

    # 配置验证
    if not args.skip_config_check:
        if not validate_config():
            console.print("[red]配置验证失败，请检查 .env 文件[/red]")
            console.print("[yellow]提示: 使用 --skip-config-check 跳过配置验证[/yellow]")
            return

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    workspace = os.path.abspath(args.workspace)
    src_dir = os.path.dirname(os.path.abspath(__file__))

    agent = Agent(workspace=workspace, client=LLMClient())
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
        plugin_manager.register_executor(run_agent)
        plugin_manager.start_all()
        agent.plugin_manager = plugin_manager

    loop = asyncio.get_running_loop()
    shutdown_event = _shutdown_event

    def signal_handler():
        logger.info("收到退出信号...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass

    try:
        if args.task:
            result = await agent.run(args.task)
            print("=======================>", result.result)
        else:
            await interactive_mode(agent)
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
