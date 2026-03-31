"""
交互模式命令处理器
"""
import logging
from typing import Optional, Tuple
from rich import box
from rich.table import Table
from rich.panel import Panel
from rich.console import Console

console = Console()
logger = logging.getLogger("agent.cmd")


class CommandHandler:
    """命令处理器 - 处理所有以 / 开头的交互命令"""

    def __init__(self, agent, session_id: str, task_queue, current_task_id: Optional[int] = None):
        self.agent = agent
        self.session_id = session_id
        self.task_queue = task_queue
        self._current_task_id = current_task_id
        self._cancel_event = None

    def set_cancel_event(self, cancel_event):
        """设置取消事件"""
        self._cancel_event = cancel_event

    def set_current_task_id(self, task_id: Optional[int]):
        """设置当前任务ID"""
        self._current_task_id = task_id

    def is_command(self, input_str: str) -> bool:
        """检查是否是命令"""
        return input_str.strip().startswith("/")

    async def handle(self, cmd: str) -> Tuple[bool, bool]:
        """
        处理命令

        Returns:
            (handled, should_continue) - 是否已处理，是否继续循环
        """
        cmd_lower = cmd.strip().lower()

        # 帮助命令
        if cmd_lower == "/help":
            self._show_help()
            return True, True

        # 系统提示词
        if cmd_lower == "/prompt":
            self._show_prompt()
            return True, True

        # 工具列表
        if cmd_lower == "/tools":
            self._show_tools()
            return True, True

        # 技能列表
        if cmd_lower == "/skills":
            self._show_skills()
            return True, True

        # 任务状态
        if cmd_lower == "/tasks":
            self._show_tasks()
            return True, True

        # 取消任务
        if cmd_lower == "/cancel":
            self._cancel_task()
            return True, True

        # 子代理列表
        if cmd_lower == "/subagents":
            self._show_subagents()
            return True, True

        # 指定模板的子代理会话
        if cmd_lower.startswith("/subagent "):
            template_name = cmd.strip()[10:].strip()
            self._show_subagent_sessions(template_name)
            return True, True

        # 所有子代理会话
        if cmd_lower == "/subagents all":
            self._show_all_subagents()
            return True, True

        # 清理子代理
        if cmd_lower == "/subagents clear":
            await self._clear_subagents()
            return True, True

        # 日志级别
        if cmd_lower.startswith("/loglevel "):
            level = cmd.strip()[10:].strip().upper()
            self._set_loglevel(level)
            return True, True

        # 缓存统计
        if cmd_lower == "/cache":
            self._show_cache()
            return True, True

        # 清空缓存
        if cmd_lower == "/cache clear":
            self._clear_cache()
            return True, True

        # 会话列表
        if cmd_lower == "/sessions":
            await self._show_sessions()
            return True, True

        # 查看指定会话
        if cmd_lower.startswith("/session "):
            target_id = cmd.strip()[9:].strip()
            await self._show_session(target_id)
            return True, True

        # 当前会话消息
        if cmd_lower.startswith("/messages"):
            await self._show_messages()
            return True, True

        # 退出
        if cmd_lower in ["quit", "exit", "/q", "/quit", "/exit"]:
            return True, False

        # 未知命令
        console.print(f"[red]未知命令: {cmd}[/red]")
        console.print("[dim]输入 /help 查看可用命令[/dim]")
        return True, True

    def _show_help(self):
        """显示帮助信息"""
        table = Table(title="可用命令", show_header=True,
                      header_style="bold cyan", box=box.ROUNDED)
        table.add_column("命令", style="yellow")
        table.add_column("说明", style="green")

        commands = [
            ("/help", "显示帮助信息"),
            ("/prompt", "查看系统提示词"),
            ("/tools", "列出可用工具"),
            ("/skills", "列出可用技能"),
            ("/tasks", "查看任务状态"),
            ("/cancel", "取消当前任务"),
            ("/subagents", "列出活跃子代理"),
            ("/subagent <模板>", "查看指定模板的子代理会话"),
            ("/subagents all", "按模板分组显示所有子代理"),
            ("/subagents clear", "清理所有子代理"),
            ("/sessions", "列出所有会话"),
            ("/session <id>", "查看指定会话详情"),
            ("/messages", "查看当前会话消息"),
            ("/loglevel <level>", "设置日志级别"),
            ("/cache", "查看缓存统计"),
            ("/cache clear", "清空缓存"),
            ("/quit /exit /q", "退出程序"),
        ]
        for cmd, desc in commands:
            table.add_row(cmd, desc)
        console.print(table)

    def _show_prompt(self):
        """显示系统提示词"""
        console.print(Panel.fit(
            f"[bold green]系统提示词:[/bold green]\n{self.agent.system_prompt}",
            border_style="green", box=box.ROUNDED
        ))

    def _show_tools(self):
        """显示工具列表"""
        table = Table(title="工具列表", show_header=True,
                      header_style="bold magenta", box=box.ROUNDED)
        table.add_column("名称", style="cyan", no_wrap=True)
        table.add_column("描述", style="green")
        for tool in self.agent.tool_defs:
            func = tool.get("function", {})
            name = func.get("name", "未知")
            desc = func.get("description", "无描述")
            # 截断过长的描述
            if len(desc) > 60:
                desc = desc[:60] + "..."
            table.add_row(name, desc)
        console.print(table)

    def _show_skills(self):
        """显示技能列表"""
        if self.agent.skill_manager:
            table = Table(title="技能列表", show_header=True,
                          header_style="bold magenta", box=box.ROUNDED)
            table.add_column("名称", style="cyan")
            for skill_name in self.agent.skill_manager.list_skills():
                table.add_row(skill_name)
            console.print(table)
        else:
            console.print("[yellow]无可用技能[/yellow]")

    def _show_tasks(self):
        """显示任务状态"""
        if self._current_task_id:
            console.print(f"[cyan]当前正在执行任务 #{self._current_task_id}[/cyan]")
        pending = self.task_queue.qsize()
        if pending > 0:
            console.print(f"[dim]队列中还有 {pending} 个任务等待[/dim]")
        else:
            console.print("[dim]队列空闲[/dim]")

    def _cancel_task(self):
        """取消当前任务"""
        if self._current_task_id:
            console.print(f"[yellow]取消任务 #{self._current_task_id}[/yellow]")
            if self._cancel_event:
                self._cancel_event.set()
        else:
            console.print("[dim]当前没有正在执行的任务[/dim]")

    def _show_subagents(self):
        """显示子代理列表"""
        if self.agent.subagent_manager:
            stats = self.agent.subagent_manager.get_stats()
            active = stats["active_subagents"]
            if active:
                table = Table(title=f"活跃子代理 (共 {len(active)} 个)", show_header=True,
                              header_style="bold magenta", box=box.ROUNDED)
                table.add_column("会话ID", style="cyan")
                table.add_column("模板", style="yellow")
                table.add_column("任务数", style="green", justify="right")
                for sub in active:
                    table.add_row(sub["session_id"], sub["template"], str(sub["task_count"]))
                console.print(table)
            else:
                console.print("[yellow]暂无活跃子代理[/yellow]")
        else:
            console.print("[yellow]子代理管理器未初始化[/yellow]")

    def _show_subagent_sessions(self, template_name: str):
        """显示指定模板的子代理会话"""
        if self.agent.subagent_manager:
            sessions = self.agent.subagent_manager.get_sessions_by_template(template_name)
            if sessions:
                table = Table(title=f"子代理 [{template_name}] 的所有会话 (共 {len(sessions)} 个)",
                              show_header=True, header_style="bold magenta", box=box.ROUNDED)
                table.add_column("会话ID", style="cyan")
                table.add_column("任务数", style="green", justify="right")
                table.add_column("Agent ID", style="yellow")
                for sess in sessions:
                    table.add_row(
                        sess["session_id"],
                        str(sess["task_count"]),
                        sess["agent_id"]
                    )
                console.print(table)
            else:
                console.print(f"[yellow]子代理 [{template_name}] 暂无活跃会话[/yellow]")
        else:
            console.print("[yellow]子代理管理器未初始化[/yellow]")

    def _show_all_subagents(self):
        """显示所有子代理"""
        if self.agent.subagent_manager:
            grouped = self.agent.subagent_manager.get_all_sessions()
            if grouped:
                for template, sessions in grouped.items():
                    table = Table(title=f"[{template}] ({len(sessions)} 个会话)",
                                  show_header=True, header_style="bold blue", box=box.ROUNDED)
                    table.add_column("会话ID", style="cyan")
                    table.add_column("任务数", style="green", justify="right")
                    table.add_column("Agent ID", style="yellow")
                    for sess in sessions:
                        table.add_row(
                            sess["session_id"],
                            str(sess["task_count"]),
                            sess["agent_id"]
                        )
                    console.print(table)
            else:
                console.print("[yellow]暂无活跃子代理[/yellow]")
        else:
            console.print("[yellow]子代理管理器未初始化[/yellow]")

    async def _clear_subagents(self):
        """清理所有子代理"""
        if self.agent.subagent_manager:
            await self.agent.subagent_manager.cleanup_all()
            console.print("[green]已清理所有子代理[/green]")

    def _set_loglevel(self, level: str):
        """设置日志级别"""
        import logging
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if level in valid_levels:
            logging.getLogger("agent").setLevel(getattr(logging, level))
            console.print(f"[green]日志级别已设置为: {level}[/green]")
        else:
            console.print(f"[red]无效的日志级别: {level}[/red]")
            console.print(f"[yellow]有效值: {', '.join(valid_levels)}[/yellow]")

    def _show_cache(self):
        """显示缓存统计"""
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

    def _clear_cache(self):
        """清空缓存"""
        from cache import get_cache
        cache = get_cache()
        cache.clear()
        console.print("[green]缓存已清空[/green]")

    async def _show_sessions(self):
        """显示会话列表"""
        if self.agent.session_manager:
            sessions = self.agent.session_manager.list_sessions()
            if sessions:
                table = Table(title=f"会话列表 (共 {len(sessions)} 个)", show_header=True,
                              header_style="bold magenta", box=box.ROUNDED)
                table.add_column("Session ID", style="cyan")
                table.add_column("消息数", style="green", justify="right")
                for sid in sessions:
                    session = await self.agent.session_manager.get_session(sid)
                    msg_count = len(session.messages) if session else 0
                    table.add_row(sid, str(msg_count))
                console.print(table)
            else:
                console.print("[yellow]暂无会话[/yellow]")
        else:
            console.print("[yellow]Session Manager 未初始化[/yellow]")

    async def _show_session(self, target_id: str):
        """显示指定会话"""
        if self.agent.session_manager:
            session = await self.agent.session_manager.get_session(target_id)
            if session:
                table = Table(title=f"会话 {target_id} (共 {len(session.messages)} 条消息)",
                              show_header=True, header_style="bold magenta", box=box.ROUNDED)
                table.add_column("#", style="dim", width=3)
                table.add_column("角色", style="cyan", width=10)
                table.add_column("内容", style="green")
                for i, msg in enumerate(session.messages, 1):
                    role = str(msg.get("role", "未知"))
                    content = str(msg.get("content", "") or "")
                    # 截断过长内容
                    if len(content) > 100:
                        content = content[:100] + "..."
                    table.add_row(str(i), role, content)
                console.print(table)
            else:
                console.print(f"[yellow]会话 {target_id} 不存在[/yellow]")
        else:
            console.print("[yellow]Session Manager 未初始化[/yellow]")

    async def _show_messages(self):
        """显示当前会话消息"""
        session = None
        if self.agent.session_manager:
            session = await self.agent.session_manager.get_session(self.session_id)
        messages = session.messages if session else []
        table = Table(title=f"当前会话消息 (共 {len(messages)} 条)",
                      show_header=True, header_style="bold magenta", box=box.ROUNDED)
        table.add_column("#", style="dim", width=3)
        table.add_column("角色", style="cyan", width=10)
        table.add_column("内容", style="green")
        for i, msg in enumerate(messages, 1):
            role = str(msg.get("role", "未知"))
            content = str(msg.get("content", "") or "")
            if len(content) > 100:
                content = content[:100] + "..."
            table.add_row(str(i), role, content)
        console.print(table)