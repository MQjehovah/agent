"""系统命令 — /help, /quit, /loglevel"""

import logging

from rich import box
from rich.table import Table


def show_help(output):
    table = Table(title="可用命令", show_header=True,
                  header_style="bold cyan", box=box.ROUNDED)
    table.add_column("命令", style="yellow")
    table.add_column("说明", style="green")
    commands = [
        ("/help", "显示帮助信息"),
        ("/prompt", "查看系统提示词"),
        ("/tools", "列出可用工具"),
        ("/skills", "列出可用技能"),
        ("/bind", "绑定插件会话到 CLI"),
        ("/unbind", "解绑插件会话"),
        ("/tasks", "查看任务状态"),
        ("/subagents", "列出活跃子代理"),
        ("/subagent <模板>", "查看指定模板的子代理会话"),
        ("/subagents all", "按模板分组显示所有子代理"),
        ("/subagents clear", "清理所有子代理"),
        ("/sessions", "列出所有会话"),
        ("/session <id>", "查看指定会话详情"),
        ("/messages", "查看当前会话消息"),
        ("/skillify [name] [desc]", "从当前会话提取工作流为可复用 Skill"),
        ("/flush", "将当前 session 决策固化到 MEMORY.md"),
        ("/dream", "跨 session 知识梦境融合"),
        ("/undo [n] [code|conversation|both]", "撤销最近 N 步操作"),
        ("/resume [id]", "恢复历史会话或列出可恢复会话"),
        ("/goal status/pause/resume/clear/history", "目标管理"),
        ("/plan on|off|approval|auto", "控制 Plan Mode"),
        ("/parallel on|off|<N>", "控制并行执行"),
        ("/loglevel <level>", "设置日志级别"),
        ("/cache", "查看缓存统计"),
        ("/cache clear", "清空缓存"),
        ("/usage", "查看 LLM 用量统计"),
        ("/panel add <任务>", "添加面板任务"),
        ("/panel rm <id>", "删除面板任务"),
        ("/panel clear", "清空面板"),
        ("/quit", "退出程序"),
    ]
    for cmd, desc in commands:
        table.add_row(cmd, desc)
    output(table)


def set_loglevel(level_str: str, output):
    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if level_str in valid_levels:
        logging.getLogger("agent").setLevel(getattr(logging, level_str))
        output(f"[green]日志级别已设置为: {level_str}[/green]")
    else:
        output(f"[red]无效的日志级别: {level_str}[/red]")
        output(f"[yellow]有效值: {', '.join(valid_levels)}[/yellow]")
