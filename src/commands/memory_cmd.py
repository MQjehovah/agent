"""记忆命令 — /flush, /dream, /skillify"""

import os

from rich import box
from rich.panel import Panel


async def flush_memory(agent, session_id: str, output):
    from storage.session_memory import SessionMemory
    if not agent:
        output("[red]Agent 未初始化[/red]")
        return
    session = None
    if agent.session_manager:
        session = await agent.session_manager.get_session(session_id)
    if not session:
        output("[red]当前会话不存在[/red]")
        return
    output("[cyan]正在固化 session 决策到记忆...[/cyan]")
    sm = SessionMemory(agent.storage, agent.agent_id, agent.workspace)
    result = await sm.flush(session.messages, agent)
    output(f"[green]✅ {result}[/green]")


async def dream_memory(agent, output):
    from storage.session_memory import SessionMemory
    if not agent:
        output("[red]Agent 未初始化[/red]")
        return
    output("[cyan]正在执行跨 session 知识梦境融合...[/cyan]")
    sm = SessionMemory(agent.storage, agent.agent_id, agent.workspace)
    result = await sm.dream(agent, agent.client)
    output(f"[green]✅ {result}[/green]")


async def skillify(agent, session_id: str, cmd: str, output):
    from worker.skillify import Skillifier
    parts = cmd.split(None, 2)
    skill_name = parts[1] if len(parts) > 1 else ""
    description = parts[2] if len(parts) > 2 else ""
    if not agent:
        output("[red]Agent 未初始化[/red]")
        return
    skills_dir = ""
    if agent.skill_manager:
        if hasattr(agent.skill_manager, 'skills_dir'):
            skills_dir = agent.skill_manager.skills_dir
    if not skills_dir and agent.config_dir:
        skills_dir = os.path.join(agent.config_dir, "skills")
    if not skills_dir:
        output("[red]技能目录未找到[/red]")
        return
    output(f"[cyan]正在从 session [{session_id}] 提取技能...[/cyan]")
    if skill_name:
        output(f"[dim]技能名称: {skill_name}[/dim]")
    skillifier = Skillifier(agent=agent, skill_manager=agent.skill_manager, skills_dir=skills_dir)
    try:
        skill_path = await skillifier.skillify(
            session_id=session_id, skill_name=skill_name,
            description=description, require_llm=True,
        )
        if skill_path:
            output(f"[green]✅ 技能已创建: {skill_path}[/green]")
            try:
                with open(skill_path, encoding="utf-8") as f:
                    content = f.read()
                output(Panel.fit(content[:1000], title="技能预览（前 1000 字符）", border_style="green"))
            except Exception:
                pass
            output("[green]使用 /skills 查看所有技能[/green]")
        else:
            output("[red]❌ 技能创建失败[/red]")
            output("[yellow]提示: 需要先执行一些包含工具调用的任务[/yellow]")
    except Exception as e:
        output(f"[red]❌ 技能提取异常: {e}[/red]")
