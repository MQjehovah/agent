"""会话命令 — /session, /sessions, /messages"""

from rich import box
from rich.table import Table


async def show_sessions(agent, output):
    if not agent.session_manager:
        output("[yellow]会话管理器未初始化[/yellow]")
        return
    info = agent.session_manager.get_session_info()
    total = info.get("total", 0)
    sessions = info.get("sessions", [])
    if not sessions:
        output("[yellow]暂无会话[/yellow]")
        return
    table = Table(title=f"会话列表 (共 {total} 个)", show_header=True,
                  header_style="bold cyan", box=box.ROUNDED)
    table.add_column("会话ID", style="cyan")
    table.add_column("Agent", style="yellow")
    table.add_column("消息数", style="green", justify="right")
    table.add_column("状态", style="magenta")
    for s in sessions:
        status = "[red]已过期[/red]" if s.get("expired") else "[green]活跃[/green]"
        table.add_row(s["id"][:20], s.get("agent_id", "?")[:12],
                      str(s["messages"]), status)
    output(table)


async def show_session(agent, session_id: str, output):
    if not agent.session_manager:
        output("[yellow]会话管理器未初始化[/yellow]")
        return
    session = await agent.session_manager.get_session(session_id)
    if not session:
        output(f"[red]会话不存在: {session_id}[/red]")
        return
    output(f"[cyan]会话 ID:[/cyan] {session.session_id}")
    output(f"[cyan]Agent ID:[/cyan] {session.agent_id}")
    output(f"[cyan]消息数:[/cyan] {len(session.messages)}")
    output(f"[cyan]创建时间:[/cyan] {session.created_at}")


async def show_messages(agent, session_id: str, output):
    if not agent.session_manager:
        output("[yellow]会话管理器未初始化[/yellow]")
        return
    session = await agent.session_manager.get_session(session_id)
    if not session:
        output(f"[red]会话不存在: {session_id}[/red]")
        return
    for msg in session.messages:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        tool_calls = msg.get("tool_calls")
        if role == "system":
            output(f"[dim]system: {content[:100]}...[/dim]")
        elif role == "user":
            output(f"[green]user: {content[:200]}[/green]")
        elif role == "assistant":
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                output(f"[cyan]assistant → tool_calls: {', '.join(names)}[/cyan]")
            elif content:
                output(f"[cyan]assistant: {content[:200]}[/cyan]")
        elif role == "tool":
            output(f"[magenta]tool({msg.get('name','?')}): {content[:100]}...[/magenta]")
