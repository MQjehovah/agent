"""
交互模式命令处理器
"""
import logging
import os
import re
from collections.abc import Callable

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")

def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)
logger = logging.getLogger("agent.cmd")


class CommandHandler:
    """命令处理器 - 处理所有以 / 开头的交互命令"""

    def __init__(self, agent, session_id: str, on_exit: Callable[[], None] = None, panel=None, output=None):
        self.agent = agent
        self.session_id = session_id
        self._current_task_id = None
        self._on_exit = on_exit
        self._panel = panel
        self._output = output

    def _print(self, *args, **kwargs):
        """Route output through TUI if available, else fallback to self._print."""
        if self._output is not None:
            from io import StringIO
            buf = StringIO()
            c = Console(file=buf, width=80)
            c.print(*args, **kwargs)
            text = buf.getvalue()
            if text.strip():
                self._output(strip_ansi(text))
        else:
            self._print(*args, **kwargs)

    def set_current_task_id(self, task_id: int | None):
        self._current_task_id = task_id

    def is_command(self, input_str: str) -> bool:
        return input_str.strip().startswith("/")

    async def handle(self, cmd: str):
        """处理命令"""
        cmd_lower = cmd.strip().lower()

        if cmd_lower == "/help":
            self._show_help()
        elif cmd_lower == "/prompt":
            self._show_prompt()
        elif cmd_lower == "/tools":
            self._show_tools()
        elif cmd_lower == "/skills":
            self._show_skills()
        elif cmd_lower == "/cancel":
            self._cancel_task()
        elif cmd_lower == "/subagents":
            self._show_subagents()
        elif cmd_lower.startswith("/subagent "):
            self._show_subagent_sessions(cmd.strip()[10:].strip())
        elif cmd_lower == "/subagents all":
            self._show_all_subagents()
        elif cmd_lower == "/subagents clear":
            await self._clear_subagents()
        elif cmd_lower.startswith("/loglevel "):
            self._set_loglevel(cmd.strip()[10:].strip().upper())
        elif cmd_lower == "/cache":
            self._show_cache()
        elif cmd_lower == "/cache clear":
            self._clear_cache()
        elif cmd_lower == "/usage":
            self._show_usage()
        elif cmd_lower == "/tasks":
            self._show_bg_tasks()
        elif cmd_lower == "/panel":
            self._show_panel()
        elif cmd_lower.startswith("/panel add "):
            self._add_panel_task(cmd.strip()[11:].strip())
        elif cmd_lower.startswith("/panel rm "):
            self._rm_panel_task(cmd.strip()[10:].strip())
        elif cmd_lower == "/panel clear":
            self._clear_panel()
        elif cmd_lower == "/sessions":
            await self._show_sessions()
        elif cmd_lower.startswith("/session "):
            await self._show_session(cmd.strip()[9:].strip())
        elif cmd_lower.startswith("/messages"):
            await self._show_messages()
        elif cmd_lower == "/bind":
            import sys
            main_mod = sys.modules.get("__main__")
            if main_mod and hasattr(main_mod, "BOUND_PLUGIN_SESSION"):
                main_mod.BOUND_PLUGIN_SESSION = getattr(main_mod, "CLI_SESSION_ID", "")
                cid = main_mod.CLI_SESSION_ID[:8] if getattr(main_mod, "CLI_SESSION_ID", "") else ""
                self._print(f"[green]插件会话已绑定到 CLI ({cid}...)[/green]")
            else:
                self._print("[red]无法获取 CLI 会话[/red]")
        elif cmd_lower.startswith("/skillify"):
            await self._skillify(cmd.strip())
        elif cmd_lower == "/flush":
            await self._flush_memory()
        elif cmd_lower == "/dream":
            await self._dream_memory()
        elif cmd_lower.startswith("/undo"):
            await self._undo(cmd.strip())
        elif cmd_lower.startswith("/resume"):
            await self._resume(cmd.strip())
        elif cmd_lower.startswith("/goal"):
            await self._goal(cmd.strip())
        elif cmd_lower == "/plan":
            self._show_plan_mode()
        elif cmd_lower.startswith("/plan "):
            await self._set_plan_mode(cmd.strip())
        elif cmd_lower == "/parallel":
            self._show_parallel_mode()
        elif cmd_lower.startswith("/parallel "):
            await self._set_parallel_mode(cmd.strip())
        elif cmd_lower == "/unbind":
            import sys
            main_mod = sys.modules.get("__main__")
            if main_mod and hasattr(main_mod, "BOUND_PLUGIN_SESSION"):
                main_mod.BOUND_PLUGIN_SESSION = ""
                self._print("[yellow]插件会话已解绑[/yellow]")
        elif cmd_lower in ["/q", "/quit", "/exit"]:
            if self._on_exit:
                self._on_exit()
        else:
            self._print(f"[red]未知命令: {cmd}[/red]")
            self._print("[dim]输入 /help 查看可用命令[/dim]")

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
            ("/bind", "绑定插件会话到 CLI（飞书/钉钉共享上下文）"),
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
            ("/undo --list", "查看可撤销的操作"),
            ("/resume [id]", "恢复历史会话或列出可恢复会话"),
            ("/goal status [id]", "查看目标状态"),
            ("/goal pause [id]", "暂停目标"),
            ("/goal resume [id]", "恢复目标"),
            ("/goal clear [id]", "清除目标"),
            ("/goal history", "查看目标历史"),
            ("/plan", "查看 Plan Mode 状态"),
            ("/plan on|off|approval|auto", "控制 Plan Mode"),
            ("/parallel", "查看并行执行状态"),
            ("/parallel on|off|<N>", "控制并行执行"),
            ("/loglevel <level>", "设置日志级别"),
            ("/cache", "查看缓存统计"),
            ("/cache clear", "清空缓存"),
            ("/usage", "查看 LLM 用量统计"),
            ("/tasks", "查看后台任务列表"),
            ("/panel add <任务>", "添加面板任务"),
            ("/panel rm <id>", "删除面板任务"),
            ("/panel clear", "清空面板"),
            ("/quit", "退出程序"),
            ("/quit", "退出程序"),
        ]
        for cmd, desc in commands:
            table.add_row(cmd, desc)
        self._print(table)

    def _show_prompt(self):
        """显示系统提示词"""
        self._print(Panel.fit(
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
            if len(desc) > 60:
                desc = desc[:60] + "..."
            table.add_row(name, desc)
        self._print(table)

    def _show_skills(self):
        """显示技能列表"""
        if self.agent.skill_manager:
            table = Table(title="技能列表", show_header=True,
                          header_style="bold magenta", box=box.ROUNDED)
            table.add_column("名称", style="cyan")
            for skill_name in self.agent.skill_manager.list_skills():
                table.add_row(skill_name)
            self._print(table)
        else:
            self._print("[yellow]无可用技能[/yellow]")

    def _show_tasks(self):
        """显示任务状态"""
        if self._current_task_id:
            self._print(f"[cyan]正在执行任务 #{self._current_task_id}[/cyan]")
        else:
            self._print("[dim]无正在执行的任务[/dim]")

    def _cancel_task(self):
        """取消当前任务"""
        self._print("[dim]使用 Ctrl+C 中断任务[/dim]")

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
                self._print(table)
            else:
                self._print("[yellow]暂无活跃子代理[/yellow]")
        else:
            self._print("[yellow]子代理管理器未初始化[/yellow]")

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
                self._print(table)
            else:
                self._print(f"[yellow]子代理 [{template_name}] 暂无活跃会话[/yellow]")
        else:
            self._print("[yellow]子代理管理器未初始化[/yellow]")

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
                    self._print(table)
            else:
                self._print("[yellow]暂无活跃子代理[/yellow]")
        else:
            self._print("[yellow]子代理管理器未初始化[/yellow]")

    async def _clear_subagents(self):
        """清理所有子代理"""
        if self.agent.subagent_manager:
            await self.agent.subagent_manager.cleanup_all()
            self._print("[green]已清理所有子代理[/green]")

    def _set_loglevel(self, level: str):
        """设置日志级别"""
        import logging
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if level in valid_levels:
            logging.getLogger("agent").setLevel(getattr(logging, level))
            self._print(f"[green]日志级别已设置为: {level}[/green]")
        else:
            self._print(f"[red]无效的日志级别: {level}[/red]")
            self._print(f"[yellow]有效值: {', '.join(valid_levels)}[/yellow]")

    # ── v2.0: /skillify ──

    async def _skillify(self, cmd: str):
        """从当前 session 自动提取工作流为可复用 Skill"""
        from skillify import Skillifier

        parts = cmd.split(None, 2)
        skill_name = parts[1] if len(parts) > 1 else ""
        description = parts[2] if len(parts) > 2 else ""

        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return

        skills_dir = ""
        if self.agent.skill_manager:
            if hasattr(self.agent.skill_manager, 'skills_dir'):
                skills_dir = self.agent.skill_manager.skills_dir
        if not skills_dir and self.agent.config_dir:
            skills_dir = os.path.join(self.agent.config_dir, "skills")

        if not skills_dir:
            self._print("[red]技能目录未找到，请指定技能路径[/red]")
            return

        self._print(f"[cyan]正在从 session [{self.session_id}] 提取技能...[/cyan]")
        if skill_name:
            self._print(f"[dim]技能名称: {skill_name}[/dim]")

        skillifier = Skillifier(
            agent=self.agent,
            skill_manager=self.agent.skill_manager,
            skills_dir=skills_dir,
        )

        try:
            skill_path = await skillifier.skillify(
                session_id=self.session_id,
                skill_name=skill_name,
                description=description,
                require_llm=True,
            )
            if skill_path:
                self._print(f"[green]✅ 技能已创建: {skill_path}[/green]")

                # 显示技能内容摘要
                try:
                    with open(skill_path, encoding="utf-8") as f:
                        content = f.read()
                    self._print(Panel.fit(
                        content[:1000],
                        title="技能预览（前 1000 字符）",
                        border_style="green",
                    ))
                except Exception:
                    pass

                # 提示加载
                skill_name_only = os.path.basename(os.path.dirname(skill_path))
                self._print(f"[green]使用 /skills 查看所有技能[/green]")
                self._print(f"[dim]提示: 下次任务匹配时将自动激活此技能[/dim]")
            else:
                self._print("[red]❌ 技能创建失败（session 可能为空或无工具调用记录）[/red]")
                self._print("[yellow]提示: 需要先执行一些包含工具调用的任务才能提取技能[/yellow]")
        except Exception as e:
            self._print(f"[red]❌ 技能提取异常: {e}[/red]")
            logger.error(f"Skillify error: {e}", exc_info=True)

    # ================================================================
    #  任务面板
    # ================================================================

    # ── v3.0: /flush & /dream ──

    async def _flush_memory(self):
        """将当前 session 决策固化到 MEMORY.md"""
        from storage.session_memory import SessionMemory

        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return

        session = None
        if self.agent.session_manager:
            session = await self.agent.session_manager.get_session(self.session_id)

        if not session or not session.messages:
            self._print("[yellow]当前 session 无消息，无法固化[/yellow]")
            return

        self._print("[cyan]正在分析 session 并提取关键决策...[/cyan]")

        memory = SessionMemory(
            client=self.agent.client,
            workspace=self.agent.workspace,
        )
        entry = await memory.flush(self.session_id, session.messages)

        if entry:
            self._print(f"[green]✅ 已固化到 MEMORY.md[/green]")
            self._print(Panel.fit(
                entry[:800],
                title="固化内容预览",
                border_style="green",
            ))
        else:
            self._print("[yellow]本次 session 未发现值得固化的内容[/yellow]")

    async def _dream_memory(self):
        """跨 session 知识融合"""
        from storage.session_memory import SessionMemory

        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return

        self._print("[cyan]正在执行知识梦境融合...[/cyan]")

        memory = SessionMemory(
            client=self.agent.client,
            workspace=self.agent.workspace,
        )
        entry = await memory.dream(recent_sessions=5)

        if entry:
            self._print(f"[green]✅ 知识梦境完成[/green]")
            self._print(Panel.fit(
                entry[:800],
                title="梦境结果预览",
                border_style="cyan",
            ))
            stats = memory.get_stats()
            self._print(f"[dim]MEMORY.md: {stats['memory_file']}, 共 {stats['flush_count']} 次固化[/dim]")
        else:
            self._print("[yellow]梦境融合未产生新内容（可能需要更多的 session 数据）[/yellow]")

    # ── v4.0: /undo ──

    async def _undo(self, cmd: str):
        """撤销最近的文件修改"""
        from undo_manager import UndoManager

        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return

        parts = cmd.split()
        steps = 1
        mode = "code"
        for p in parts[1:]:
            if p.lstrip("-").isdigit():
                steps = int(p)
            elif p in ("code", "conversation", "both"):
                mode = p

        undo_mgr = getattr(self.agent, '_undo_manager', None)
        if not undo_mgr:
            undo_mgr = UndoManager(self.agent.workspace)
            self.agent._undo_manager = undo_mgr

        # 先查看历史
        if steps == 0 or "--list" in cmd:
            history = undo_mgr.get_history(limit=10)
            if history:
                table = Table(title="可撤销操作", show_header=True, header_style="bold cyan", box=box.ROUNDED)
                table.add_column("ID", style="dim")
                table.add_column("时间", style="yellow")
                table.add_column("工具", style="green")
                table.add_column("文件", style="white")
                for h in history:
                    files_str = ", ".join(h["files"][:3])
                    if len(h["files"]) > 3:
                        files_str += f" +{len(h['files'])-3}"
                    table.add_row(h["id"], h["time_ago"], h["tool"], files_str)
                self._print(table)
            else:
                self._print("[yellow]无可撤销的操作[/yellow]")
            return

        self._print(f"[cyan]正在撤销 {steps} 步...[/cyan]")
        result = await undo_mgr.undo(steps=steps, mode=mode)
        files = result.get("files_restored", [])
        if files:
            for f in files:
                self._print(f"  ↩️ [green]恢复: {f}[/green]")
            self._print(f"[green]✅ 已撤销 {len(files)} 个文件的修改[/green]")
        else:
            self._print("[yellow]没有需要恢复的文件[/yellow]")

    # ── v4.0: /resume ──

    async def _resume(self, cmd: str):
        """恢复历史会话"""
        from storage.resume_manager import ResumeManager

        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return

        parts = cmd.split()
        session_id = parts[1] if len(parts) > 1 else ""

        if not session_id:
            # 列出可恢复的 session
            rm = ResumeManager(
                storage=getattr(self.agent, 'storage', None),
                session_manager=self.agent.session_manager,
            )
            sessions = await rm.list_sessions(limit=10)
            if sessions:
                table = Table(title="可恢复的会话", show_header=True, header_style="bold cyan", box=box.ROUNDED)
                table.add_column("Session ID", style="cyan")
                table.add_column("消息数", style="green")
                for s in sessions:
                    sid = s.get("session_id", "")
                    msgs = len(self.agent.storage.get_messages(sid)) if self.agent.storage else 0
                    table.add_row(sid[:12], str(msgs))
                self._print(table)
                self._print("[dim]使用 /resume <session_id> 恢复[/dim]")
            else:
                self._print("[yellow]无可恢复的会话[/yellow]")
            return

        rm = ResumeManager(
            storage=getattr(self.agent, 'storage', None),
            session_manager=self.agent.session_manager,
        )
        context = await rm.resume_session(session_id)
        self._print(f"[green]✅ 已加载 session {session_id[:8]} 的上下文[/green]")
        self._print(Panel.fit(context[:600], title="恢复的上下文", border_style="green"))

    # ── v4.0: /goal ──

    async def _goal(self, cmd: str):
        """管理自治目标"""
        from storage.resume_manager import GoalLifecycle

        parts = cmd.split()
        subcmd = parts[1].lower() if len(parts) > 1 else "status"

        storage = getattr(self.agent, 'storage', None)
        gm = GoalLifecycle(storage=storage)

        if subcmd == "status":
            if len(parts) > 2:
                status = gm.get_status(parts[2])
            else:
                status = gm.get_status()
            self._print(f"[cyan]目标状态:[/cyan]")
            for k, v in status.items():
                self._print(f"  {k}: {v}")
            history = gm.get_history(limit=5)
            if history:
                self._print(f"\n[dim]历史目标:[/dim]")
                for h in history:
                    self._print(f"  {h['id']}: {h['title']} [{h['status']}]")

        elif subcmd == "pause":
            goal_id = parts[2] if len(parts) > 2 else ""
            if await gm.pause(goal_id):
                self._print(f"[yellow]⏸️ 目标已暂停: {goal_id or '当前'}[/yellow]")
            else:
                self._print(f"[red]暂停失败: 未找到目标[/red]")

        elif subcmd == "resume":
            goal_id = parts[2] if len(parts) > 2 else ""
            context = await gm.resume(goal_id)
            if context:
                self._print(f"[green]▶️ 目标已恢复: {context.get('title', '')}[/green]")
                self._print(f"[dim]进度: {context.get('progress', '')}[/dim]")
                if context.get("next_step"):
                    self._print(f"[dim]下一步: {context['next_step']}[/dim]")
            else:
                self._print(f"[red]恢复失败: 未找到目标[/red]")

        elif subcmd == "clear":
            goal_id = parts[2] if len(parts) > 2 else ""
            await gm.clear(goal_id)
            self._print(f"[yellow]🗑️ 目标已清除: {goal_id or '全部'}[/yellow]")

        elif subcmd == "history":
            history = gm.get_history(limit=20)
            if history:
                table = Table(title="目标历史", show_header=True, header_style="bold cyan", box=box.ROUNDED)
                table.add_column("ID", style="dim")
                table.add_column("标题", style="white")
                table.add_column("状态", style="yellow")
                table.add_column("步骤", style="green")
                for h in history:
                    table.add_row(h["id"], h["title"], h["status"], str(h["steps"]))
                self._print(table)
            else:
                self._print("[yellow]无目标历史[/yellow]")

        else:
            self._print(f"[red]未知子命令: {subcmd}[/red]")
            self._print("[dim]/goal status | /goal pause [id] | /goal resume [id] | /goal clear [id] | /goal history[/dim]")

    def _show_panel(self):
        if self._panel is None:
            self._print("[yellow]任务面板仅在自主模式下可用[/yellow]")
            return
        tasks = self._panel.list_all()
        if not tasks:
            self._print(Panel.fit(
                "[dim]任务面板为空[/dim]\n"
                "使用 [cyan]/panel add <任务>[/cyan] 添加\n"
                "启动时已根据角色自动生成，部分纯响应型角色无需主动任务",
                border_style="dim", box=box.ROUNDED
            ))
            return
        stats = self._panel.get_stats()
        table = Table(
            title=f"任务面板 ({stats['total']}个 | {stats['pending']}pending {stats['active']}active {stats['completed']}done)",
            show_header=True, header_style="bold cyan", box=box.ROUNDED
        )
        table.add_column("ID", style="dim", width=12)
        table.add_column("状态", style="cyan", width=8)
        table.add_column("P", style="yellow", width=3)
        table.add_column("源", style="magenta", width=4)
        table.add_column("间隔", style="green", width=8)
        table.add_column("标题", style="white")
        for t in tasks:
            sc = {"pending": "yellow", "active": "cyan", "completed": "green"}.get(t.status, "dim")
            itv = f"{t.interval}s" if t.interval else "一次"
            src = {"user": "U", "llm": "AI", "event": "E"}.get(t.source, t.source)
            table.add_row(t.id, f"[{sc}]{t.status}[/{sc}]", str(t.priority), src, itv, t.title)
        self._print(table)
        self._print("[dim]/panel add <任务> | /panel rm <id> | /panel clear[/dim]")

    def _add_panel_task(self, text: str):
        if self._panel is None:
            self._print("[yellow]任务面板仅在自主模式下可用[/yellow]")
            return
        if not text:
            self._print("[red]用法: /panel add <任务标题>[/red]")
            return
        task = self._panel.add_task(title=text, source="user")
        self._print(f"[green]已添加: [{task.id}] {text}[/green]")

    def _rm_panel_task(self, task_id: str):
        if self._panel is None:
            self._print("[yellow]任务面板仅在自主模式下可用[/yellow]")
            return
        if self._panel.remove_task(task_id):
            self._print(f"[green]已删除: {task_id}[/green]")
        else:
            self._print(f"[red]未找到: {task_id}[/red]")

    def _clear_panel(self):
        if self._panel is None:
            self._print("[yellow]任务面板仅在自主模式下可用[/yellow]")
            return
        for t in list(self._panel.list_all()):
            self._panel.remove_task(t.id)
        self._print("[green]面板已清空[/green]")

    def _show_cache(self):
        """显示缓存统计"""
        from llm.cache import get_cache
        cache = get_cache()
        stats = cache.get_stats()
        table = Table(title="缓存统计", show_header=True,
                      header_style="bold magenta", box=box.ROUNDED)
        table.add_column("指标", style="cyan")
        table.add_column("值", style="green")
        table.add_row("缓存大小", f"{stats['size']}/{stats['max_size']}")
        table.add_row("总命中次数", str(stats['total_hits']))
        self._print(table)

    def _clear_cache(self):
        """清空缓存"""
        from llm.cache import get_cache
        cache = get_cache()
        cache.clear()
        self._print("[green]缓存已清空[/green]")

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
                self._print(table)
            else:
                self._print("[yellow]暂无会话[/yellow]")
        else:
            self._print("[yellow]Session Manager 未初始化[/yellow]")

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
                    if len(content) > 100:
                        content = content[:100] + "..."
                    table.add_row(str(i), role, content)
                self._print(table)
            else:
                self._print(f"[yellow]会话 {target_id} 不存在[/yellow]")
        else:
            self._print("[yellow]Session Manager 未初始化[/yellow]")

    async def _show_messages(self):
        """显示当前会话消息（含子代理消息）"""
        from storage.storage import get_storage
        storage = get_storage()
        all_msgs = []
        # 从 memory 收集当前会话消息
        if self.agent.session_manager:
            sess = await self.agent.session_manager.get_session(self.session_id)
            if sess and sess.messages:
                for m in sess.messages:
                    if m.get("role") != "system":
                        all_msgs.append(("", m))
        # 从 storage 收集子会话消息
        if storage and self.agent.subagent_manager:
            try:
                recent = storage.list_recent_sessions(limit=100)
                prefix = f"{self.session_id}:"
                for s in recent:
                    sid = s.get("session_id", "")
                    if sid.startswith(prefix):
                        label = sid.split(":", 1)[1] if ":" in sid else sid[:8]
                        msgs = storage.get_messages(sid) or []
                        for m in msgs:
                            role = m.get("role", "")
                            if role == "system":
                                continue
                            # 子 session 的 user 消息是 orchestrator 内部指令，不显示
                            if label and role == "user":
                                continue
                            all_msgs.append((label, m))
            except Exception as e:
                import logging
                logging.getLogger("agent.cmd").debug(f"读取子会话消息失败: {e}")
        print(f"\n  [消息] 共 {len(all_msgs)} 条")
        if all_msgs:
            for i, (label, m) in enumerate(all_msgs[-30:], 1):
                role = str(m.get("role", "?"))
                content = str(m.get("content", "") or "")[:150]
                prefix = f"[{label}] " if label else ""
                print(f"  {i:>3}. {prefix}{role}: {content}")

    def _show_usage(self):
        """显示 LLM 用量统计"""
        if not (hasattr(self.agent, 'client') and hasattr(self.agent.client, 'usage_tracker')):
            self._print("[yellow]用量追踪未启用[/yellow]")
            return

        tracker = self.agent.client.usage_tracker
        summary = tracker.get_summary()

        # ── 总览 ──
        overview = Table(title="LLM 用量统计", show_header=True,
                         header_style="bold magenta", box=box.ROUNDED)
        overview.add_column("指标", style="cyan")
        overview.add_column("值", style="green")
        overview.add_row("调用次数", str(summary["total_calls"]))
        overview.add_row("输入 Token", f"{summary['total_prompt_tokens']:,}")
        overview.add_row("输出 Token", f"{summary['total_completion_tokens']:,}")
        overview.add_row("总 Token", f"{summary['total_tokens']:,}")
        overview.add_row("总费用", f"¥{summary['total_cost_cny']}")

        avg_ms = summary["avg_duration_ms"]
        if avg_ms > 0:
            overview.add_row("平均耗时", f"{avg_ms:,.0f} ms")
            overview.add_row("最长耗时", f"{summary['max_duration_ms']:,.0f} ms")
            overview.add_row("最短耗时", f"{summary['min_duration_ms']:,.0f} ms")
            total_sec = summary["total_duration_ms"] / 1000
            overview.add_row("总耗时", f"{total_sec:,.1f} s")
            if summary["total_completion_tokens"] > 0 and total_sec > 0:
                speed = summary["total_completion_tokens"] / total_sec
                overview.add_row("输出速度", f"{speed:,.1f} tokens/s")
        self._print(overview)

        # ── 按模型分布 ──
        per_model = tracker.get_per_model_summary()
        if len(per_model) > 1:
            model_table = Table(title="按模型分布", show_header=True,
                                header_style="bold magenta", box=box.ROUNDED)
            model_table.add_column("模型", style="cyan")
            model_table.add_column("调用", style="green", justify="right")
            model_table.add_column("输入", style="green", justify="right")
            model_table.add_column("输出", style="green", justify="right")
            model_table.add_column("总计", style="green", justify="right")
            model_table.add_column("费用", style="yellow", justify="right")
            model_table.add_column("耗时", style="dim", justify="right")
            for model_name, ms in per_model.items():
                dur_sec = ms["duration_ms"] / 1000
                model_table.add_row(
                    model_name,
                    str(ms["calls"]),
                    f"{ms['prompt_tokens']:,}",
                    f"{ms['completion_tokens']:,}",
                    f"{ms['prompt_tokens'] + ms['completion_tokens']:,}",
                    f"¥{ms['cost']:.4f}",
                    f"{dur_sec:.1f}s",
                )
            self._print(model_table)

        # ── 上下文统计 ──
        if hasattr(self.agent, 'tracer'):
            ctx_stats = self.agent.tracer.get_context_stats()
            if ctx_stats["samples"] > 0:
                ctx_table = Table(title="上下文 Token 统计", show_header=True,
                                  header_style="bold magenta", box=box.ROUNDED)
                ctx_table.add_column("指标", style="cyan")
                ctx_table.add_column("值", style="green")
                ctx_table.add_row("采样次数", str(ctx_stats["samples"]))
                ctx_table.add_row("峰值", f"{ctx_stats['peak']:,}")
                ctx_table.add_row("最终值", f"{ctx_stats['final']:,}")
                ctx_table.add_row("平均值", f"{ctx_stats['avg']:,}")
                self._print(ctx_table)

    # ── v2.0: /plan ──

    def _show_plan_mode(self):
        """显示 Plan Mode 状态"""
        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return
        enabled = getattr(self.agent, '_enable_plan_mode', False)
        require_approval = getattr(self.agent, '_plan_mode_config', {}).get("require_approval", True)
        plan_mode = getattr(self.agent, '_plan_mode', None)
        status = "🟢 已启用" if enabled else "🔴 已禁用"
        approval = "需要审批" if require_approval else "自动通过"
        self._print(f"[cyan]Plan Mode: {status}[/cyan]")
        self._print(f"[dim]审批模式: {approval}[/dim]")
        if plan_mode and plan_mode.current_plan:
            plan = plan_mode.current_plan
            self._print(f"[yellow]当前计划: {plan.title}[/yellow]")
            self._print(f"[dim]步骤数: {len(plan.steps)}, 状态: {plan.status}[/dim]")
        else:
            self._print("[dim]当前无活跃计划[/dim]")
        self._print("[dim]/plan on | /plan off | /plan approval | /plan auto[/dim]")

    async def _set_plan_mode(self, cmd: str):
        """设置 Plan Mode"""
        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return
        parts = cmd.split(None, 1)
        setting = parts[1].lower() if len(parts) > 1 else ""
        if setting == "on":
            self.agent._enable_plan_mode = True
            if not self.agent._plan_mode:
                from plan_mode import PlanMode
                self.agent._plan_mode = PlanMode(
                    client=self.agent.client,
                    workspace=self.agent.workspace,
                    on_confirm=self.agent.on_confirm,
                )
            self._print("[green]✅ Plan Mode 已启用[/green]")
        elif setting == "off":
            self.agent._enable_plan_mode = False
            self._print("[yellow]Plan Mode 已禁用[/yellow]")
        elif setting == "approval":
            if not hasattr(self.agent, '_plan_mode_config'):
                self.agent._plan_mode_config = {}
            self.agent._plan_mode_config["require_approval"] = True
            self._print("[green]✅ 计划需审批[/green]")
        elif setting == "auto":
            if not hasattr(self.agent, '_plan_mode_config'):
                self.agent._plan_mode_config = {}
            self.agent._plan_mode_config["require_approval"] = False
            self._print("[yellow]计划自动通过（无需审批）[/yellow]")
        else:
            self._print(f"[red]未知设置: {setting}[/red]")
            self._print("[dim]/plan on | /plan off | /plan approval | /plan auto[/dim]")

    # ── v2.0: /parallel ──

    def _show_parallel_mode(self):
        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return
        enabled = getattr(self.agent, '_enable_parallel', False)
        max_p = getattr(self.agent, '_max_parallel', 4)
        pool = getattr(self.agent, '_agent_pool', None)
        status = "🟢 已启用" if enabled else "🔴 已禁用"
        pool_info = f", Agent 池大小: {pool.total_created}, 空闲: {pool.idle_count}, 忙碌: {pool.busy_count}" if pool else ", Agent 池: 未创建"
        self._print(f"[cyan]并行模式: {status}[/cyan]")
        self._print(f"[dim]最大并行度: {max_p}{pool_info}[/dim]")
        self._print("[dim]/parallel on | /parallel off | /parallel N (设置并行度)[/dim]")

    async def _set_parallel_mode(self, cmd: str):
        if not self.agent:
            self._print("[red]Agent 未初始化[/red]")
            return
        parts = cmd.split(None, 1)
        setting = parts[1].lower() if len(parts) > 1 else ""
        if setting == "on":
            self.agent._enable_parallel = True
            self._print("[green]✅ 并行模式已启用[/green]")
        elif setting == "off":
            self.agent._enable_parallel = False
            self._print("[yellow]并行模式已禁用（回退到串行）[/yellow]")
        elif setting.isdigit():
            n = int(setting)
            if 1 <= n <= 16:
                self.agent._max_parallel = n
                self._print(f"[green]✅ 最大并行度已设为 {n}[/green]")
            else:
                self._print("[red]并行度范围: 1-16[/red]")
        else:
            self._print(f"[red]未知设置: {setting}[/red]")
            self._print("[dim]/parallel on | /parallel off | /parallel N[/dim]")

    def _show_bg_tasks(self):
        """显示后台任务列表"""
        if hasattr(self.agent, 'task_manager'):
            tasks = self.agent.task_manager.list_tasks()
            if tasks:
                table = Table(title=f"后台任务 (共 {len(tasks)} 个)", show_header=True,
                              header_style="bold magenta", box=box.ROUNDED)
                table.add_column("ID", style="cyan")
                table.add_column("描述", style="green")
                table.add_column("状态", style="yellow")
                table.add_column("创建时间", style="dim")
                for t in tasks:
                    table.add_row(t["id"], t["description"], t["status"], t["created_at"])
                self._print(table)
            else:
                self._print("[dim]暂无后台任务[/dim]")
        else:
            self._print("[yellow]任务管理器未初始化[/yellow]")
