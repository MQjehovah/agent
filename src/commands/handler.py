"""
交互模式命令处理器 — 路由到 commands/ 各模块
"""
import logging
import re
from collections.abc import Callable

from rich.console import Console

import commands.system_cmd
import commands.agent_cmd
import commands.session_cmd
import commands.task_cmd
import commands.memory_cmd
import commands.sysadmin_cmd
import commands.panel_cmd

_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


logger = logging.getLogger("agent.cmd")


class CommandHandler:
    """命令处理器 - 路由到 commands/ 各模块"""

    def __init__(self, agent, session_id: str, on_exit: Callable[[], None] = None, panel=None, output=None):
        self.agent = agent
        self.session_id = session_id
        self._current_task_id = None
        self._on_exit = on_exit
        self._panel = panel
        self._output = output

    def _print(self, *args, **kwargs):
        if self._output is not None:
            from io import StringIO
            buf = StringIO()
            c = Console(file=buf, width=80)
            c.print(*args, **kwargs)
            text = buf.getvalue()
            if text.strip():
                self._output(strip_ansi(text))
        else:
            print(*args, **kwargs)

    def _out(self):
        if self._output:
            return lambda text: self._output(strip_ansi(text)) if text.strip() else None
        return lambda text: print(text) if text else None

    def set_current_task_id(self, task_id: int | None):
        self._current_task_id = task_id

    def is_command(self, input_str: str) -> bool:
        return input_str.strip().startswith("/")

    async def handle(self, cmd: str):
        cmd_lower = cmd.strip().lower()
        out = self._out()
        a = self.agent
        sid = self.session_id

        if cmd_lower == "/help":
            commands.system_cmd.show_help(out)
        elif cmd_lower == "/prompt":
            commands.agent_cmd.show_prompt(a, out)
        elif cmd_lower == "/tools":
            commands.agent_cmd.show_tools(a, out)
        elif cmd_lower == "/skills":
            commands.agent_cmd.show_skills(a, out)
        elif cmd_lower == "/cancel":
            out("[dim]使用 Ctrl+C 中断任务[/dim]")
        elif cmd_lower == "/subagents":
            commands.agent_cmd.show_subagents(a, out)
        elif cmd_lower.startswith("/subagent "):
            commands.agent_cmd.show_subagent_sessions(a, cmd.strip()[10:].strip(), out)
        elif cmd_lower == "/subagents all":
            commands.agent_cmd.show_all_subagents(a, out)
        elif cmd_lower == "/subagents clear":
            await commands.agent_cmd.clear_subagents(a, out)
        elif cmd_lower.startswith("/loglevel "):
            commands.system_cmd.set_loglevel(cmd.strip()[10:].strip().upper(), out)
        elif cmd_lower == "/cache":
            commands.task_cmd.show_cache(a, out)
        elif cmd_lower == "/cache clear":
            commands.task_cmd.clear_cache(a, out)
        elif cmd_lower == "/usage":
            commands.task_cmd.show_usage(a, out)
        elif cmd_lower == "/tasks":
            commands.task_cmd.show_bg_tasks(a, out)
        elif cmd_lower == "/panel":
            commands.panel_cmd.show_panel(a, out)
        elif cmd_lower.startswith("/panel add "):
            await commands.panel_cmd.add_panel_task(a, cmd.strip()[11:].strip(), out)
        elif cmd_lower.startswith("/panel rm "):
            await commands.panel_cmd.rm_panel_task(a, cmd.strip()[10:].strip(), out)
        elif cmd_lower == "/panel clear":
            await commands.panel_cmd.clear_panel(a, out)
        elif cmd_lower == "/sessions":
            await commands.session_cmd.show_sessions(a, out)
        elif cmd_lower.startswith("/session "):
            await commands.session_cmd.show_session(a, cmd.strip()[9:].strip(), out)
        elif cmd_lower.startswith("/messages"):
            await commands.session_cmd.show_messages(a, sid, out)
        elif cmd_lower == "/bind":
            commands.agent_cmd.bind_session(out)
        elif cmd_lower == "/unbind":
            commands.agent_cmd.unbind_session(out)
        elif cmd_lower.startswith("/skillify"):
            await commands.memory_cmd.skillify(a, sid, cmd.strip(), out)
        elif cmd_lower == "/flush":
            await commands.memory_cmd.flush_memory(a, sid, out)
        elif cmd_lower == "/dream":
            await commands.memory_cmd.dream_memory(a, out)
        elif cmd_lower.startswith("/undo"):
            await commands.sysadmin_cmd.undo(a, cmd.strip(), out)
        elif cmd_lower.startswith("/resume"):
            await commands.sysadmin_cmd.resume(a, cmd.strip(), out)
        elif cmd_lower.startswith("/goal"):
            await commands.sysadmin_cmd.goal(a, cmd.strip(), out)
        elif cmd_lower == "/plan":
            commands.sysadmin_cmd.show_plan_mode(a, out)
        elif cmd_lower.startswith("/plan "):
            await commands.sysadmin_cmd.set_plan_mode(a, cmd.strip(), out)
        elif cmd_lower == "/parallel":
            commands.sysadmin_cmd.show_parallel_mode(a, out)
        elif cmd_lower.startswith("/parallel "):
            await commands.sysadmin_cmd.set_parallel_mode(a, cmd.strip(), out)
        elif cmd_lower in ["/q", "/quit", "/exit"]:
            if self._on_exit:
                self._on_exit()
        else:
            out(f"[red]未知命令: {cmd}[/red]")
            out("[dim]输入 /help 查看可用命令[/dim]")
