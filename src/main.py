import os
import sys
import asyncio
import logging
import json
from typing import Dict, Any, List, Optional

from rich.logging import RichHandler
from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt
from rich.console import Console

from agent import Agent
from llm_client import LLMClient

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


async def interactive_mode(agent: Agent):
    logger.info("进入交互模式")
    
    while True:
        try:
            question = await asyncio.get_event_loop().run_in_executor(
                None, lambda: Prompt.ask(
                    "\n[bold cyan]?[/bold cyan] [cyan]请描述任务[/cyan]")
            )
        except:
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

        if question.strip().lower() == "/messages":
            table = Table(title=f"当前会话消息 (共 {len(agent.messages)} 条)",
                          show_header=True, header_style="bold magenta", box=box.ROUNDED)
            table.add_column("#", style="dim", width=3)
            table.add_column("角色", style="cyan", width=10)
            table.add_column("内容", style="green")
            for i, msg in enumerate(agent.messages, 1):
                role = str(msg.get("role", "未知"))
                content = str(msg.get("content", "") or "")
                table.add_row(str(i), role, content)
            console.print(table)
            continue

        if question.strip().lower() == "/skills":
            if agent.skill_manager:
                console.print(Panel.fit(
                    f"[bold green]技能列表:[/bold green]\n{agent.skill_manager.list_skills()}",
                    border_style="green", box=box.ROUNDED
                ))
            else:
                console.print("[yellow]无可用技能[/yellow]")
            continue

        if question.strip().lower() in ["quit", "exit", "q"]:
            logger.info("ℹ 再见!")
            break

        console.print()
        result = await agent.run(question)

        console.print(Panel.fit(
            f"[bold green]执行结果:[/bold green]\n{result.result}",
            border_style="green", box=box.ROUNDED
        ))


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", "-t", help="执行单个任务")
    parser.add_argument("--workspace", "-w", default="config", help="Agent工作目录")
    parser.add_argument("--debug", action="store_true", help="启用调试模式")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    logger.info("启动 Agent")
    
    workspace = os.path.join(os.path.curdir, args.workspace)
    client = LLMClient()
    
    agent = Agent(workspace=workspace, client=client)
    await agent.initialize()

    if args.task:
        result = await agent.run(args.task)
        print(result.result)
    else:
        await interactive_mode(agent)

    await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())