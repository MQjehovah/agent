import os
import sys
import uuid
import asyncio
import logging
import signal
from typing import Dict, Any, List, Optional
from pathlib import Path

from rich import box
from rich.text import Text
from rich.table import Table
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


async def interactive_mode(agent: Agent):
    global _shutdown_event
    logger.info(f"进入交互模式")
    session_id = str(uuid.uuid4())
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

        if question.strip().lower().startswith("/loglevel "):
            level = question.strip()[10:].strip().upper()
            valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            if level in valid_levels:
                logging.getLogger("agent").setLevel(getattr(logging, level))
                console.print(f"[green]日志级别已设置为: {level}[/green]")
            else:
                console.print(f"[red]无效的日志级别: {level}[/red]")
                console.print(f"[yellow]有效值: {', '.join(valid_levels)}[/yellow]")
            continue

        if question.strip().lower() == "/cache":
            from cache import get_cache
            cache = get_cache()
            stats = cache.get_stats()
            table = Table(title="缓存统计", show_header=True,
                          header_style="bold magenta", box=box.ROUNDED)
            table.add_column("指标", style="cyan")
            table.add_column("值", style="green")
            table.add_row("缓存大小", f"{stats['size']}/{stats['max_size']}")
            table.add_row("总命中次数", str(stats['total_hits']))
            console.print(table)
            continue

        if question.strip().lower() == "/cache clear":
            from cache import get_cache
            cache = get_cache()
            cache.clear()
            console.print("[green]缓存已清空[/green]")
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
        result = await agent.run(question, session_id=session_id)  # 交互模式下的会话

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
