import asyncio
import contextlib
import logging
import sys
import threading
import time

from .layout import ChatLayout
from .output import Display, _fmt_args, _truncate
from .status import StatusBar
from .styles import CYAN, DIM, GRAY, GREEN, RESET

logger = logging.getLogger("agent.tui")

_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _monitor_escape(cancel_flag: threading.Event):
    last_esc = 0.0
    try:
        if sys.platform == "win32":
            import ctypes
            vk_escape = 0x1B
            user32 = ctypes.windll.user32
            while not cancel_flag.is_set():
                if user32.GetAsyncKeyState(vk_escape) & 1:
                    now = time.time()
                    if now - last_esc < 1.0:
                        cancel_flag.set()
                        break
                    last_esc = now
                time.sleep(0.05)
        else:
            import select
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                while not cancel_flag.is_set():
                    r, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch == '\x1b':
                            now = time.time()
                            if now - last_esc < 1.0:
                                cancel_flag.set()
                                break
                            last_esc = now
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception as e:
        logger.debug(f"ESC monitor thread error: {e}")


class TUIState:
    def __init__(self):
        self.task_active = False
        self.task_start_ts = 0.0
        self.round = 0
        self.tool_count = 0
        self.tool_name = ""
        self.tool_args = ""
        self.current_stage = ""
        self.agent_name = ""
        self.model_name = ""
        self.ctx_tokens = 0
        self.iter_count = 0
        self.stage_max_iter = 0
        self.total_prompt = 0
        self.total_completion = 0
        self.total_cost = 0.0


class TUIApp:
    """Full-screen chat TUI coordinator."""

    def __init__(self, agent=None):
        self.state = TUIState()
        self.status_bar = StatusBar()
        agent_name = agent.name if agent else ""
        self.chat = ChatLayout(self.status_bar, agent_name=agent_name)
        self.display = Display(self.chat.append_output)
        self.agent = agent
        self._shutdown = asyncio.Event()
        self._cancel_flag = threading.Event()
        self._input_queue: asyncio.Queue[str] = asyncio.Queue()
        self._app_task: asyncio.Task | None = None
        self._spinner_task: asyncio.Task | None = None
        self._context = ""
        self._branch = ""
        self._target_agent = ""
        self._session_id = ""
        self._hook_unregisters = []

        self.chat.on_submit(self._on_input_submit)
        self.chat.on_cancel(lambda: self._cancel_flag.set())
        self.chat.on_exit(lambda: self._shutdown.set())

    def _on_input_submit(self, text: str):
        self.chat.append_output(f"  {GREEN}❯{RESET} {text}")
        self._input_queue.put_nowait(text)

    # ── setup ────────────────────────────────────────────────────

    def setup(self, workspace_context: str, branch: str = "",
              target_agent: str = "", session_id: str = ""):
        self._context = workspace_context
        self._branch = branch
        self._target_agent = target_agent
        self._session_id = session_id

    def register_hooks(self, agent):
        from hooks import HookEvent

        def _on_tool_start(ctx):
            if self._target_agent:
                return
            self.state.tool_count += 1
            self.state.tool_name = ctx.tool_name
            self.state.tool_args = _fmt_args(ctx.arguments or {})
            self.state.task_active = True

        def _on_tool_result(ctx):
            pass

        def _on_round_start(ctx):
            it = (ctx.metadata or {}).get("iteration", 0)
            self.state.round = it

        def _on_subagent_start(ctx):
            meta = ctx.metadata or {}
            name = meta.get("name", "?")
            self.state.agent_name = name
            self.state.tool_name = ""
            self.state.tool_args = ""
            self.chat.append_output(f"  {DIM}▶ 子代理 {name} 启动{RESET}")

        def _on_subagent_round_start(ctx):
            it = (ctx.metadata or {}).get("iteration", 0)
            self.state.round = it

        def _on_subagent_result(ctx):
            meta = ctx.metadata or {}
            self.state.agent_name = self._target_agent or (
                getattr(self.agent, 'name', None) or "")
            self.display.subagent_result(
                meta.get("name", "?"),
                meta.get("status", "?"),
                meta.get("result", ""),
            )

        def _on_subagent_tool_start(ctx):
            if self._target_agent:
                return
            self.state.tool_count += 1
            self.state.tool_name = ctx.tool_name
            self.state.tool_args = _fmt_args(ctx.arguments or {})

        def _on_subagent_tool_result(ctx):
            pass

        def _on_subagent_progress(ctx):
            meta = ctx.metadata or {}
            stage = meta.get("stage", "")
            status = meta.get("status", "")
            info = meta.get("info")
            extra = meta.get("extra")
            if status == "_ctx" and isinstance(info, dict):
                self.state.ctx_tokens = info.get("tokens", 0)
                self.state.iter_count = info.get("iter", 0)
            elif status == "start" and info:
                self.state.current_stage = stage
                self.state.agent_name = info
                self.state.iter_count = 0
                self.state.stage_max_iter = extra if isinstance(extra, (int, float)) else 0

        def _on_llm_response(ctx):
            reasoning = ctx.reasoning or ""
            if reasoning:
                for line in reasoning.strip().split("\n")[-2:]:
                    t = _truncate(line, 100)
                    if t:
                        self.chat.append_output(f"  {DIM}┊ {t}{RESET}")

        def _on_chat_event(ctx):
            pass

        def _on_subagent_llm_response(ctx):
            content = ctx.content or ""
            reasoning = ctx.reasoning or ""
            lines = []
            if reasoning:
                for line in reasoning.strip().split("\n")[-2:]:
                    if line.strip():
                        lines.append(f"  {DIM}┊ {line.strip()[:120]}{RESET}")
            if content:
                lines.append(f"  {DIM}┊ {content.strip()[:120]}{RESET}")
            if lines:
                self.chat.append_output("\n".join(lines))

        def _on_subagent_chat_event(ctx):
            pass

        def _on_agent_start(ctx):
            self.state.task_active = True

        def _on_agent_stop(ctx):
            self.state.task_active = False
            self._update_token_stats()

        agent.hooks.register(HookEvent.TOOL_START, _on_tool_start)
        agent.hooks.register(HookEvent.TOOL_RESULT, _on_tool_result)
        agent.hooks.register(HookEvent.ROUND_START, _on_round_start)
        agent.hooks.register(HookEvent.LLM_RESPONSE, _on_llm_response)
        agent.hooks.register(HookEvent.CHAT_EVENT, _on_chat_event)
        agent.hooks.register(HookEvent.SUBAGENT_START, _on_subagent_start)
        agent.hooks.register(HookEvent.SUBAGENT_ROUND_START, _on_subagent_round_start)
        agent.hooks.register(HookEvent.SUBAGENT_RESULT, _on_subagent_result)
        agent.hooks.register(HookEvent.SUBAGENT_TOOL_START, _on_subagent_tool_start)
        agent.hooks.register(HookEvent.SUBAGENT_TOOL_RESULT, _on_subagent_tool_result)
        agent.hooks.register(HookEvent.SUBAGENT_CHAT_EVENT, _on_subagent_chat_event)
        agent.hooks.register(HookEvent.SUBAGENT_LLM_RESPONSE, _on_subagent_llm_response)
        agent.hooks.register(HookEvent.SUBAGENT_PROGRESS, _on_subagent_progress)
        agent.hooks.register("agent_start", _on_agent_start)
        agent.hooks.register("agent_stop", _on_agent_stop)

        self._hook_unregisters = [
            (HookEvent.TOOL_START, _on_tool_start),
            (HookEvent.TOOL_RESULT, _on_tool_result),
            (HookEvent.ROUND_START, _on_round_start),
            (HookEvent.SUBAGENT_START, _on_subagent_start),
            (HookEvent.SUBAGENT_RESULT, _on_subagent_result),
            (HookEvent.SUBAGENT_TOOL_START, _on_subagent_tool_start),
            (HookEvent.SUBAGENT_TOOL_RESULT, _on_subagent_tool_result),
            (HookEvent.SUBAGENT_PROGRESS, _on_subagent_progress),
            ("agent_start", _on_agent_start),
            ("agent_stop", _on_agent_stop),
        ]

    def setup_ask_handler(self, ask_tool):
        if ask_tool and hasattr(ask_tool, "set_input_handler"):
            handler = self._handle_ask_user
            ask_tool.set_input_handler(handler)

    async def _handle_ask_user(self, question: str, options: list, default: str) -> str:
        self.status_bar.set_waiting(question)
        self.chat.update_status()
        self.display.ask_question(question, options, default)
        self.chat.start_ask(options, default)
        self.chat.input_locked = False
        try:
            text = await self._input_queue.get()
        except asyncio.CancelledError:
            self.chat.end_ask()
            return default or ""
        self.chat.end_ask()
        return text or default or ""

    # ── lifecycle ───────────────────────────────────────────────

    async def start(self):
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        self._update_token_stats()
        self.status_bar.set_idle(
            context=self._context, branch=self._branch,
            tokens=self._fmt_tokens(), cost=self._fmt_cost(),
            session_id=self._session_id,
        )
        self._app_task = asyncio.create_task(self.chat.application.run_async())

    def _set_input_locked(self, locked: bool):
        self.chat.input_locked = locked
        self.chat.update_status()

    def start_task(self):
        self.state.task_active = True
        self.state.task_start_ts = time.time()
        self.state.round = 0
        self.state.tool_count = 0
        self.state.tool_name = ""
        self.state.tool_args = ""
        self.state.current_stage = ""
        self.state.agent_name = self._target_agent or (
            getattr(self.agent, 'name', None) or "")
        self.state.model_name = getattr(self.agent.client, "model", "") if self.agent else ""
        self.state.ctx_tokens = 0
        self.state.iter_count = 0
        self._cancel_flag.clear()
        self._set_input_locked(True)
        self._start_esc_monitor()

    def after_task(self, result_text: str = ""):
        self.state.task_active = False
        elapsed = self._elapsed_str()
        self._stop_esc_monitor()
        if result_text:
            self.display.result_text(result_text, elapsed)
        self._update_token_stats()
        self.status_bar.set_idle(
            context=self._context, branch=self._branch,
            tokens=self._fmt_tokens(), cost=self._fmt_cost(),
            session_id=self._session_id,
        )
        self._set_input_locked(False)

    def cancel_notice(self):
        self.state.task_active = False
        self._stop_esc_monitor()
        self.display.cancel_notice()
        self.status_bar.set_idle(
            context=self._context, branch=self._branch,
            tokens=self._fmt_tokens(), cost=self._fmt_cost(),
            session_id=self._session_id,
        )
        self._set_input_locked(False)

    def error_notice(self, msg: str):
        self.state.task_active = False
        self._stop_esc_monitor()
        self.display.error(msg)
        self.status_bar.set_idle(
            context=self._context, branch=self._branch,
            tokens=self._fmt_tokens(), cost=self._fmt_cost(),
            session_id=self._session_id,
        )
        self._set_input_locked(False)

    def update_status(self):
        if self.state.task_active:
            elapsed = time.time() - self.state.task_start_ts
            self.status_bar.set_running(
                elapsed=elapsed, round_num=self.state.round,
                agent_name=self.state.agent_name, stage=self.state.current_stage,
                tool_count=self.state.tool_count, tool_name=self.state.tool_name,
                tool_args=self.state.tool_args, ctx_tokens=self.state.ctx_tokens,
                model=self.state.model_name, tokens=self._fmt_tokens(),
                cost=self._fmt_cost(),
            )
        self.chat.update_status()

    # ── public input api ────────────────────────────────────────

    async def get_input(self) -> str | None:
        self._set_input_locked(False)
        while not self._shutdown.is_set():
            try:
                text = await asyncio.wait_for(self._input_queue.get(), timeout=0.5)
                return text
            except asyncio.TimeoutError:
                continue
            except (asyncio.CancelledError, RuntimeError):
                return None
        return None

    # ── spinner ─────────────────────────────────────────────────

    async def run_spinner(self):
        while self.state.task_active and not self._shutdown.is_set():
            self.update_status()
            elapsed = time.time() - self.state.task_start_ts
            ch = _SPINNER_CHARS[int(time.time() * 10) % 10]
            parts = [f"{DIM}{ch}{RESET}", f"{DIM}{elapsed:.0f}s{RESET}"]
            if self.state.current_stage:
                parts.append(f"{CYAN}{self.state.current_stage}{RESET}")
            elif self.state.round:
                parts.append(f"{DIM}round {self.state.round}{RESET}")
            if self.state.agent_name and self.state.agent_name != self.state.current_stage:
                parts.append(f"{DIM}{self.state.agent_name}{RESET}")
            if self.state.model_name:
                parts.append(f"{GRAY}[{self.state.model_name[:20]}]{RESET}")
            if self.state.tool_count:
                parts.append(f"{GRAY}{self.state.tool_count}t{RESET}")
            if self.state.ctx_tokens:
                parts.append(f"{GRAY}ctx {self.state.ctx_tokens:,}{RESET}")
            total = self.state.total_prompt + self.state.total_completion
            if total:
                parts.append(f"{GRAY}∑{total:,}{RESET}")
            if self.state.stage_max_iter and self.state.iter_count:
                parts.append(f"{GRAY}{self.state.iter_count}/{self.state.stage_max_iter}{RESET}")
            elif self.state.iter_count:
                parts.append(f"{GRAY}r{self.state.iter_count}{RESET}")
            self.chat.update_status()
            await asyncio.sleep(0.15)

    def start_spinner(self):
        self._spinner_task = asyncio.create_task(self.run_spinner())

    async def stop_spinner(self):
        if self._spinner_task and not self._spinner_task.done():
            self._spinner_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._spinner_task
            self._spinner_task = None

    # ── helpers ─────────────────────────────────────────────────

    def _start_esc_monitor(self):
        t = threading.Thread(
            target=_monitor_escape, args=(self._cancel_flag,), daemon=True)
        t.start()

    def _stop_esc_monitor(self):
        self._cancel_flag.set()

    @property
    def cancel_flag(self):
        return self._cancel_flag

    def _elapsed_str(self) -> str:
        t = time.time() - self.state.task_start_ts
        if t < 60:
            return f"{t:.0f}s"
        return f"{t // 60:.0f}m{t % 60:.0f}s"

    def _update_token_stats(self):
        if not self.agent:
            return
        try:
            u = self.agent.client.usage_tracker.get_summary() if hasattr(
                self.agent.client, 'usage_tracker') else {}
            self.state.total_prompt = u.get("total_prompt_tokens", 0)
            self.state.total_completion = u.get("total_completion_tokens", 0)
            self.state.total_cost = u.get("total_cost_cny", 0.0)
        except Exception:
            pass

    def _fmt_tokens(self) -> str:
        total = self.state.total_prompt + self.state.total_completion
        return f"{total:,}" if total else ""

    def _fmt_cost(self) -> str:
        return f"¥{self.state.total_cost:.2f}" if self.state.total_cost else ""

    def shutdown(self):
        self._shutdown.set()
        self._cancel_flag.set()
        with contextlib.suppress(Exception):
            self.chat.application.exit()

    @property
    def is_shutdown(self):
        return self._shutdown.is_set()
