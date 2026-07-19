"""Agent 命令 — /prompt, /tools, /skills, /bind, /unbind, /subagents"""

import sys

from rich import box
from rich.panel import Panel
from rich.table import Table


def show_prompt(agent, output):
    output(Panel.fit(
        f"[bold green]系统提示词:[/bold green]\n{agent.system_prompt}",
        border_style="green", box=box.ROUNDED
    ))


def show_tools(agent, output):
    table = Table(title="工具列表", show_header=True,
                  header_style="bold magenta", box=box.ROUNDED)
    table.add_column("名称", style="cyan", no_wrap=True)
    table.add_column("描述", style="green")
    for tool in agent.tool_defs:
        func = tool.get("function", {})
        name = func.get("name", "未知")
        desc = func.get("description", "无描述")
        if len(desc) > 60:
            desc = desc[:60] + "..."
        table.add_row(name, desc)
    output(table)


def show_skills(agent, output):
    if agent.skill_manager:
        table = Table(title="技能列表", show_header=True,
                      header_style="bold magenta", box=box.ROUNDED)
        table.add_column("名称", style="cyan")
        for skill_name in agent.skill_manager.list_skills():
            table.add_row(skill_name)
        output(table)
    else:
        output("[yellow]无可用技能[/yellow]")


def bind_session(output):
    main_mod = sys.modules.get("__main__")
    if main_mod and hasattr(main_mod, "BOUND_PLUGIN_SESSION"):
        main_mod.BOUND_PLUGIN_SESSION = getattr(main_mod, "CLI_SESSION_ID", "")
        cid = main_mod.CLI_SESSION_ID[:8] if getattr(main_mod, "CLI_SESSION_ID", "") else ""
        output(f"[green]插件会话已绑定到 CLI ({cid}...)[/green]")
    else:
        output("[red]无法获取 CLI 会话[/red]")


def unbind_session(output):
    main_mod = sys.modules.get("__main__")
    if main_mod and hasattr(main_mod, "BOUND_PLUGIN_SESSION"):
        main_mod.BOUND_PLUGIN_SESSION = ""
        output("[yellow]插件会话已解绑[/yellow]")


def show_subagents(agent, output):
    if agent.subagent_manager:
        stats = agent.subagent_manager.get_stats()
        active = stats["active_subagents"]
        if active:
            table = Table(title=f"活跃子代理 (共 {len(active)} 个)",
                          show_header=True, header_style="bold magenta", box=box.ROUNDED)
            table.add_column("会话ID", style="cyan")
            table.add_column("模板", style="yellow")
            table.add_column("任务数", style="green", justify="right")
            for sub in active:
                table.add_row(sub["session_id"], sub["template"], str(sub["task_count"]))
            output(table)
        else:
            output("[yellow]暂无活跃子代理[/yellow]")
    else:
        output("[yellow]子代理管理器未初始化[/yellow]")


def show_subagent_sessions(agent, template_name: str, output):
    if agent.subagent_manager:
        sessions = agent.subagent_manager.get_sessions_by_template(template_name)
        if sessions:
            table = Table(title=f"子代理 [{template_name}] 的所有会话 (共 {len(sessions)} 个)",
                          show_header=True, header_style="bold magenta", box=box.ROUNDED)
            table.add_column("会话ID", style="cyan")
            table.add_column("任务数", style="green", justify="right")
            table.add_column("Agent ID", style="yellow")
            for sess in sessions:
                table.add_row(sess["session_id"], str(sess["task_count"]), sess["agent_id"])
            output(table)
        else:
            output(f"[yellow]子代理 [{template_name}] 暂无活跃会话[/yellow]")
    else:
        output("[yellow]子代理管理器未初始化[/yellow]")


def show_all_subagents(agent, output):
    if agent.subagent_manager:
        grouped = agent.subagent_manager.get_all_sessions()
        if grouped:
            for template, sessions in grouped.items():
                table = Table(title=f"[{template}] ({len(sessions)} 个会话)",
                              show_header=True, header_style="bold blue", box=box.ROUNDED)
                table.add_column("会话ID", style="cyan")
                table.add_column("任务数", style="green", justify="right")
                table.add_column("Agent ID", style="yellow")
                for sess in sessions:
                    table.add_row(sess["session_id"], str(sess["task_count"]), sess["agent_id"])
                output(table)
        else:
            output("[yellow]暂无活跃子代理[/yellow]")
    else:
        output("[yellow]子代理管理器未初始化[/yellow]")


async def clear_subagents(agent, output):
    if agent.subagent_manager:
        await agent.subagent_manager.cleanup_all()
        output("[green]已清理所有子代理[/green]")
