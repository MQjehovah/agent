import asyncio
import contextlib
import logging
import os
import signal
import time
import uuid

from agent.core import Agent
from channels import MessageRouter
from commands.handler import CommandHandler
from tui import TUIApp
from tui.display import _fmt_args, _truncate
from tui.styles import BOLD, CYAN, DIM, GRAY, GREEN, RED, RESET, YELLOW

logger = logging.getLogger("agent.main")


async def interactive_mode(agent: Agent, shutdown_event: asyncio.Event, target_agent: str = ""):
    """交互模式 — target_agent 不为空时直接路由到子代理"""
    router = MessageRouter(agent)
    session_id = router.format_session_id("cli", uuid.uuid4().hex[:12])

    ws_context = os.path.basename(os.path.normpath(agent.workspace))
    branch = ""
    try:
        import subprocess as _sp
        branch = _sp.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                  cwd=agent.workspace, stderr=_sp.DEVNULL, timeout=3).decode().strip()
    except Exception:
        pass
    if target_agent:
        ws_context += f" → {target_agent}"

    tui = TUIApp(agent)
    tui.setup(ws_context, branch, target_agent, session_id)
    tui.register_hooks(agent)

    ask_tool = agent.tool_registry.get_tool("ask_user") if agent.tool_registry else None
    tui.setup_ask_handler(ask_tool)

    cmd_handler = CommandHandler(agent, session_id, on_exit=shutdown_event.set,
                                  output=tui.chat.append_output)
    current_task: asyncio.Task | None = None

    await tui.start()

    def _team_progress(stage, status, info, extra=None):
        now = time.time()
        if status == "start":
            tui.state.current_stage = stage
            tui.state.agent_name = info
            tui.chat.append_output(f"  {DIM}{'─' * 40}{RESET}")
            tui.chat.append_output(f"  {BOLD}{CYAN}{stage}{RESET}  {GRAY}({info}){RESET}")
        elif status == "pipeline":
            tui.chat.append_output(f"  {DIM}pipeline: {', '.join(info)}{RESET}")
        elif status == "feedback":
            tui.chat.append_output(f"  {DIM}{'─' * 40}{RESET}")
            tui.chat.append_output(f"  {BOLD}{YELLOW}↻{RESET} 开发↔测试反馈循环 {GRAY}{info}{RESET}")
            if extra:
                tui.chat.append_output(f"  {DIM}  · 失败详情: {GRAY}{extra[:120]}{RESET}")
        elif status == "stage_timeout":
            tui.chat.append_output(f"  {DIM}  {YELLOW}⚠{RESET} {GRAY}timeout{RESET}")
        elif status == "stage_done":
            parts = stage.split("|", 1)
            name = parts[0]
            tui.state.current_stage = ""
            tui.chat.append_output(f"  {DIM}  {GREEN}✔{RESET} {name}  {GRAY}{now - tui.state.task_start_ts:.0f}s{RESET}")
        elif status == "llm":
            text = str(info or "").strip()
            if text:
                first = text.split("\n")[0].strip()[:120]
                if first:
                    tui.chat.append_output(f"  {DIM}  · {GRAY}{first}{RESET}")
        elif status == "tool_start":
            tname = stage.split("|", 1)[0] if "|" in stage else stage
            brief = _fmt_args(info) if info else ""
            tui.chat.append_output(f"  {DIM}  · {tname} {GRAY}{brief}{RESET}")
        elif status == "tool_result":
            brief = _truncate(extra or "", 45)
            if brief and (brief.startswith('{"success": false') or "错误" in brief or "失败" in brief):
                tui.chat.append_output(f"  {DIM}  · {RED}✗{RESET} {DIM}{brief[:60]}{RESET}")

    def handle_signal():
        shutdown_event.set()
        tui.shutdown()
        if current_task and not current_task.done():
            current_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, handle_signal)

    try:
        while not shutdown_event.is_set() and not tui.is_shutdown:
            question = await tui.get_input()
            if question is None:
                break

            if not question.strip():
                continue

            if cmd_handler.is_command(question):
                await cmd_handler.handle(question)
                continue

            task_counter = 0 if not hasattr(interactive_mode, '_counter') else interactive_mode._counter
            task_counter += 1
            interactive_mode._counter = task_counter

            async def _run_task(task_id: int, question: str):
                nonlocal current_task
                cmd_handler.set_current_task_id(task_id)
                tui.start_task()
                tui.start_spinner()

                try:
                    target = target_agent
                    if target and agent.factory:
                        if target in agent.factory._team_configs:
                            team_dir = os.path.join(agent.config_dir, "agents", target)
                            team_agent = Agent(
                                workspace=agent.workspace, config_dir=team_dir,
                                client=agent.client, parent_agent=agent,
                                permission_mode=getattr(agent, '_permission_config', None) and
                                agent._permission_config.mode.value or "auto",
                            )
                            team_agent.factory = agent.factory
                            team_agent._progress_callback = _team_progress
                            await team_agent.initialize()
                            result = await team_agent.run(
                                question, session_id=session_id,
                                user_id="cli:admin", user_name="管理员",
                            )
                        else:
                            sub_agent, sub_sid = await agent.factory.create(
                                name=target, client=agent.client,
                                parent_agent=agent,
                            )
                            result = await sub_agent.run(question)
                    else:
                        result = await agent.run(question, session_id=session_id,
                                                  user_id="cli:admin", user_name="管理员")
                    await tui.stop_spinner()
                    text = result.result if hasattr(result, "result") else str(result)
                    tui.after_task(text)
                except asyncio.CancelledError:
                    await tui.stop_spinner()
                    logger.warning("任务被用户取消")
                    tui.cancel_notice()
                except Exception as e:
                    await tui.stop_spinner()
                    logger.exception(f"任务执行异常: {e}")
                    tui.error_notice(str(e))
                finally:
                    cmd_handler.set_current_task_id(None)

            current_task = asyncio.create_task(_run_task(task_counter, question))

            while not current_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(current_task), timeout=0.3)
                    break
                except asyncio.TimeoutError:
                    if tui.cancel_flag.is_set() and not current_task.done():
                        current_task.cancel()
                        break
                    if shutdown_event.is_set():
                        if not current_task.done():
                            current_task.cancel()
                        break
                    continue

            current_task = None

    finally:
        tui.shutdown()
        if current_task and not current_task.done():
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await current_task
