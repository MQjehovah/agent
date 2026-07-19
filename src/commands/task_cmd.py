"""任务/缓存命令 — /tasks, /cache, /usage"""

from rich import box
from rich.table import Table


def show_bg_tasks(agent, output):
    if not hasattr(agent, 'task_manager') or not agent.task_manager:
        output("[yellow]任务管理器未初始化[/yellow]")
        return
    tasks = agent.task_manager.list_tasks()
    if not tasks:
        output("[yellow]暂无后台任务[/yellow]")
        return
    table = Table(title="后台任务", show_header=True,
                  header_style="bold magenta", box=box.ROUNDED)
    table.add_column("ID", style="cyan")
    table.add_column("描述", style="green")
    table.add_column("状态", style="yellow")
    for t in tasks:
        table.add_row(t["id"], t.get("description", "")[:40], t["status"])
    output(table)


def show_cache(agent, output):
    if hasattr(agent, 'client') and agent.client and hasattr(agent.client, '_cache'):
        cache = agent.client._cache
        output(f"[cyan]缓存统计:[/cyan]")
        output(f"  大小: {cache.size()} / {cache.max_size}")
        output(f"  TTL: {cache.ttl} 秒")
    else:
        output("[yellow]缓存未启用[/yellow]")


def clear_cache(agent, output):
    if hasattr(agent, 'client') and agent.client and hasattr(agent.client, '_cache'):
        agent.client._cache.clear()
        output("[green]缓存已清空[/green]")
    else:
        output("[yellow]缓存未启用[/yellow]")


def show_usage(agent, output):
    if not agent.client or not hasattr(agent.client, 'usage_tracker'):
        output("[yellow]用量统计未启用[/yellow]")
        return
    tracker = agent.client.usage_tracker
    u = tracker.get_summary()
    table = Table(title="LLM 用量统计", show_header=True,
                  header_style="bold cyan", box=box.ROUNDED)
    table.add_column("指标", style="yellow")
    table.add_column("数值", style="green", justify="right")
    items = [
        ("调用次数", str(u.get("total_calls", 0))),
        ("输入 Token", f"{u.get('total_prompt_tokens', 0):,}"),
        ("输出 Token", f"{u.get('total_completion_tokens', 0):,}"),
        ("总 Token", f"{u.get('total_tokens', 0):,}"),
        ("总费用 (CNY)", f"¥{u.get('total_cost_cny', 0):.4f}"),
    ]
    for name, val in items:
        table.add_row(name, val)
    output(table)

    if hasattr(agent, 'tracer'):
        cs = agent.tracer.get_context_stats()
        ctx_table = Table(title="上下文 Token 统计", show_header=True,
                          header_style="bold cyan", box=box.ROUNDED)
        ctx_table.add_column("指标", style="yellow")
        ctx_table.add_column("数值", style="green", justify="right")
        ctx_items = [
            ("采样次数", str(cs.get("samples", 0))),
            ("峰值", f"{cs.get('peak', 0):,}"),
            ("最终值", f"{cs.get('final', 0):,}"),
            ("平均值", f"{cs.get('avg', 0):,}"),
        ]
        for name, val in ctx_items:
            ctx_table.add_row(name, val)
        output(ctx_table)
