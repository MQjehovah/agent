"""系统管理命令 — /undo, /resume, /goal, /plan, /parallel"""


async def undo(agent, cmd: str, output):
    from worker.undo_manager import UndoManager
    parts = cmd.split()
    mode = "both"
    steps = 1
    if len(parts) > 1:
        for p in parts[1:]:
            if p.isdigit():
                steps = int(p)
            elif p in ("code", "conversation", "both"):
                mode = p
    if not agent or not agent.workspace:
        output("[red]Agent 或工作目录未设置[/red]")
        return
    mgr = UndoManager(agent.workspace)
    if steps <= 0:
        result = mgr.list_snapshots()
        output(f"[cyan]可撤销的操作:[/cyan]\n{result[:1000]}")
        return
    result = await mgr.undo(steps, mode)
    output(f"[green]{result}[/green]")


async def resume(agent, cmd: str, output):
    from storage.resume_manager import ResumeManager
    if not agent:
        output("[red]Agent 未初始化[/red]")
        return
    mgr = ResumeManager(agent.storage, agent.workspace)
    parts = cmd.split(None, 1)
    session_id = parts[1] if len(parts) > 1 else ""
    result = await mgr.resume_session(session_id)
    output(f"[green]{result}[/green]")


async def goal(agent, cmd: str, output):
    from storage.resume_manager import ResumeManager
    if not agent:
        output("[red]Agent 未初始化[/red]")
        return
    mgr = ResumeManager(agent.storage, agent.workspace)
    parts = cmd.split(None, 2)
    action = parts[1] if len(parts) > 1 else "status"
    goal_id = parts[2] if len(parts) > 2 else ""
    result = await mgr.handle_goal(action, goal_id, agent)
    output(f"[green]{result}[/green]")


def show_plan_mode(agent, output):
    if hasattr(agent, '_plan_mode') and agent._plan_mode is not None:
        output(f"[cyan]Plan Mode: {agent._plan_mode}[/cyan]")
    else:
        output("[yellow]Plan Mode 未启用[/yellow]")


async def set_plan_mode(agent, cmd: str, output):
    parts = cmd.split(None, 1)
    value = parts[1].strip().lower() if len(parts) > 1 else ""
    if value in ("on", "off", "approval", "auto"):
        if hasattr(agent, '_set_plan_mode'):
            await agent._set_plan_mode(value)
            output(f"[green]Plan Mode 已设置为: {value}[/green]")
        else:
            output("[red]Plan Mode 不可用[/red]")
    else:
        output("[red]无效参数，使用 on/off/approval/auto[/red]")


def show_parallel_mode(agent, output):
    if hasattr(agent, '_enable_parallel'):
        status = "开启" if agent._enable_parallel else "关闭"
        mx = getattr(agent, '_max_parallel', 4)
        output(f"[cyan]并行执行: {status}, 最大并行数: {mx}[/cyan]")
    else:
        output("[yellow]并行执行不可用[/yellow]")


async def set_parallel_mode(agent, cmd: str, output):
    parts = cmd.split(None, 1)
    value = parts[1].strip().lower() if len(parts) > 1 else ""
    if value in ("on", "off"):
        agent._enable_parallel = value == "on"
        output(f"[green]并行执行已{'开启' if value == 'on' else '关闭'}[/green]")
    elif value.isdigit():
        n = int(value)
        agent._max_parallel = max(1, min(n, 10))
        agent._enable_parallel = True
        output(f"[green]并行执行已开启，最大并行数: {agent._max_parallel}[/green]")
    else:
        output("[red]无效参数，使用 on/off/<并行数>[/red]")
