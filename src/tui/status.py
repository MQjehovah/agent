import html
import time

from prompt_toolkit.formatted_text import HTML


class StatusBar:
    """Manages the bottom toolbar (status bar) content for PromptSession."""

    def __init__(self):
        self.mode = "idle"   # idle | running | waiting
        self._context = ""
        self._branch = ""
        self._question = ""
        self._elapsed = 0
        self._round = 0
        self._tokens = ""
        self._cost = ""
        self._agent_name = ""
        self._stage = ""
        self._tool_count = 0
        self._ctx_tokens = 0
        self._model = ""
        self._session_id = ""
        self._tool_name = ""
        self._tool_args = ""

    def set_idle(self, context: str = "", branch: str = "", tokens: str = "",
                 cost: str = "", session_id: str = "", ctx_tokens: int = 0):
        self.mode = "idle"
        self._context = context
        self._branch = branch
        self._tokens = tokens
        self._cost = cost
        self._session_id = session_id
        self._ctx_tokens = ctx_tokens

    def set_running(self, elapsed: float = 0, round_num: int = 0,
                    agent_name: str = "", stage: str = "", tool_count: int = 0,
                    ctx_tokens: int = 0, model: str = "", tokens: str = "",
                    cost: str = "", tool_name: str = "", tool_args: str = ""):
        self.mode = "running"
        self._elapsed = elapsed
        self._round = round_num
        self._agent_name = agent_name
        self._stage = stage
        self._tool_count = tool_count
        self._ctx_tokens = ctx_tokens
        self._model = model
        self._tokens = tokens
        self._cost = cost
        self._tool_name = tool_name
        self._tool_args = tool_args

    def set_waiting(self, question: str = ""):
        self.mode = "waiting"
        self._question = question

    def render(self):
        if self.mode == "idle":
            return self._render_idle()
        elif self.mode == "running":
            return self._render_running()
        elif self.mode == "waiting":
            return self._render_waiting()
        return HTML("")

    def _render_idle(self):
        parts = ['<style fg="ansiwhite">●</style> <style fg="ansiwhite">Idle</style>']
        ctx = html.escape(self._context)
        if self._branch:
            ctx += f' <style fg="gray">({html.escape(self._branch)})</style>'
        parts.append(f'<style fg="gray">{ctx}</style>')
        right = []
        safe_tokens = self._tokens or "0"
        safe_cost = self._cost or "¥0"
        if self._ctx_tokens:
            right.append(f'ctx {self._ctx_tokens:,}')
        elif safe_tokens:
            right.append(f'ctx {safe_tokens}')
        if self._session_id:
            right.append(f'session: {html.escape(self._session_id[:12])}')
        right.append(f'∑{safe_tokens}')
        right.append(f'{html.escape(safe_cost)}')
        parts.append(f'<style fg="gray">|</style> {" ".join(right)}')
        return HTML("  ".join(parts))

    def _render_running(self):
        elapsed_str = f"{self._elapsed:.0f}s" if self._elapsed < 60 else f"{self._elapsed // 60:.0f}m{self._elapsed % 60:.0f}s"
        spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 10) % 10]
        parts = [f'<style fg="yellow">{spinner}</style> '
                 f'<style fg="ansiwhite">Running</style>']
        info = [elapsed_str]
        if self._stage:
            info.append(html.escape(self._stage))
        elif self._round:
            info.append(f'round {self._round}')
        if self._agent_name:
            info.append(html.escape(self._agent_name))
        if self._tool_count:
            info.append(f'{self._tool_count} tools')
        if self._ctx_tokens:
            info.append(f'ctx {self._ctx_tokens:,}')
        if self._model:
            info.append(html.escape(self._model[:20]))
        parts.append(f'<style fg="gray">{" · ".join(info)}</style>')
        right = []
        if self._tool_name:
            label = html.escape(self._tool_name)
            if self._tool_args:
                label += f" {html.escape(self._tool_args)}"
            right.append(label)
        safe_tokens = self._tokens or "0"
        safe_cost = self._cost or "¥0"
        right.append(f'∑{safe_tokens}')
        right.append(f'{html.escape(safe_cost)}')
        parts.append(f'<style fg="gray">|</style> {" ".join(right)}')
        return HTML("  ".join(parts))

    def _render_waiting(self):
        q = html.escape(self._question[:50] + "..." if len(self._question) > 50 else self._question)
        return HTML(f'<style fg="cyan">●</style> <style fg="ansiwhite">Waiting</style> <style fg="gray">{q}</style>')
