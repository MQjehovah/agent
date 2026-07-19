"""面板命令 — /panel, /panel add, /panel rm, /panel clear"""


def show_panel(agent, output):
    if hasattr(agent, '_panel') and agent._panel:
        output(f"[cyan]当前面板: {agent._panel}[/cyan]")
    else:
        output("[yellow]面板未配置[/yellow]")


async def add_panel_task(agent, task_text: str, output):
    if hasattr(agent, '_add_panel_task'):
        await agent._add_panel_task(task_text)
        output(f"[green]已添加面板任务: {task_text}[/green]")
    else:
        output("[red]面板不可用[/red]")


async def rm_panel_task(agent, task_id: str, output):
    if hasattr(agent, '_rm_panel_task'):
        await agent._rm_panel_task(task_id)
        output(f"[green]已移除面板任务: {task_id}[/green]")
    else:
        output("[red]面板不可用[/red]")


async def clear_panel(agent, output):
    if hasattr(agent, '_clear_panel'):
        await agent._clear_panel()
        output("[green]面板已清空[/green]")
    else:
        output("[red]面板不可用[/red]")
