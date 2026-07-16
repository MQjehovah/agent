import re

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import to_filter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.containers import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout

_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class ChatLayout:
    """Full-screen chat layout with scrollable output, input line, and status bar."""

    def __init__(self, status_bar, agent_name: str = ""):
        self.status_bar = status_bar
        self._agent_name = agent_name or "Zero Agent"
        self._submit_callback = None
        self._cancel_callback = None
        self._exit_callback = None
        self._input_locked = False

        # Output — NOT read_only so cursor movement scrolls
        self._output_lines: list[str] = []
        self._output_buffer = Buffer()

        # Input
        from .input import _COMMANDS
        self._input_buffer = Buffer(
            multiline=False,
            completer=WordCompleter(_COMMANDS, match_middle=False),
            complete_while_typing=True,
        )

        # Ask-mode: ↑↓ select, Enter confirm
        self._ask_active = False
        self._ask_options: list[str] = []
        self._ask_selected = 0

        self._kb = self._build_key_bindings()
        self._app = self._build_application()

    # ── ask-mode ───────────────────────────────────────────────

    def start_ask(self, options: list[str], default: str = ""):
        self._ask_active = bool(options)
        self._ask_options = options
        self._ask_selected = 0
        if options:
            if default and default in options:
                self._ask_selected = options.index(default)
            self._input_buffer.text = options[self._ask_selected]

    def end_ask(self):
        self._ask_active = False
        self._ask_options = []
        self._ask_selected = 0
        self._input_buffer.text = ""

    @property
    def is_asking(self) -> bool:
        return self._ask_active

    # ── key bindings ──────────────────────────────────────────

    def _build_key_bindings(self):
        kb = KeyBindings()

        @kb.add("c-c")
        def _exit(event):
            event.app.exit()
            if self._exit_callback:
                self._exit_callback()

        @kb.add("c-d")
        def _eof(event):
            if not self._input_buffer.text:
                event.app.exit()

        @kb.add("escape", "escape")
        def _cancel(event):
            if self._cancel_callback:
                self._cancel_callback()

        @kb.add("up")
        def _up(event):
            if self._ask_active and self._ask_options:
                self._ask_selected = max(0, self._ask_selected - 1)
                self._input_buffer.text = self._ask_options[self._ask_selected]
                return
            # Normal: let input buffer handle history / cursor
            event.app.layout.current_buffer.cursor_up(1)

        @kb.add("down")
        def _down(event):
            if self._ask_active and self._ask_options:
                self._ask_selected = min(len(self._ask_options) - 1, self._ask_selected + 1)
                self._input_buffer.text = self._ask_options[self._ask_selected]
                return
            event.app.layout.current_buffer.cursor_down(1)

        @kb.add("pageup")
        def _page_up(event):
            self._output_buffer.cursor_up(10)

        @kb.add("pagedown")
        def _page_down(event):
            self._output_buffer.cursor_down(10)

        @kb.add(Keys.ScrollUp)
        def _scroll_up(event):
            self._output_buffer.cursor_up(3)

        @kb.add(Keys.ScrollDown)
        def _scroll_down(event):
            self._output_buffer.cursor_down(3)

        @kb.add("enter")
        def _submit(event):
            if self._input_locked:
                return
            text = self._input_buffer.text
            if self._ask_active and self._ask_options:
                text = self._ask_options[self._ask_selected]
                self._input_buffer.text = ""
                if self._submit_callback:
                    self._submit_callback(text)
                return
            if not text.strip():
                return
            self._input_buffer.text = ""
            if self._submit_callback:
                self._submit_callback(text)

        return kb

    # ── properties ────────────────────────────────────────────

    @property
    def input_locked(self):
        return self._input_locked

    @input_locked.setter
    def input_locked(self, value: bool):
        self._input_locked = value
        self._input_buffer.read_only = to_filter(value)

    def on_submit(self, callback):
        self._submit_callback = callback

    def on_cancel(self, callback):
        self._cancel_callback = callback

    def on_exit(self, callback):
        self._exit_callback = callback

    # ── output ────────────────────────────────────────────────

    def _rebuild_output(self):
        all_text = "\n".join(self._output_lines)
        self._output_buffer.set_document(Document(all_text, len(all_text)))

    def append_output(self, text: str = ""):
        self._output_lines.append(strip_ansi(text))
        self._rebuild_output()
        self._app.invalidate()

    def update_status(self):
        self._app.invalidate()

    # ── build ─────────────────────────────────────────────────

    def _build_application(self):
        header = Window(
            FormattedTextControl(
                HTML(f'<style fg="ansicyan" bold="true"> ◆ {self._agent_name}</style>'
                     f' <style fg="gray">│ ↑↓PgUp/PgDn滚动 │ Enter发送</style>'),
            ),
            height=1,
            style="bg:#ansibrightblack",
        )

        output_window = Window(
            BufferControl(buffer=self._output_buffer, focusable=False),
            wrap_lines=True,
            always_hide_cursor=True,
        )

        separator = Window(
            FormattedTextControl(
                lambda: HTML(f'<style fg="gray" bold="true">{"─" * 80}</style>'),
            ),
            height=1,
        )

        def _prefix():
            if self._input_locked:
                return HTML('<style fg="gray">⏳</style> ')
            if self._ask_active:
                return HTML('<style fg="cyan">❯</style> ')
            return HTML('<style fg="green">❯</style> ')

        input_area = VSplit([
            Window(FormattedTextControl(_prefix), width=4, height=1,
                   dont_extend_width=True),
            Window(BufferControl(buffer=self._input_buffer, focusable=True),
                   height=1, dont_extend_height=True),
        ])

        status_line = Window(
            FormattedTextControl(self.status_bar.render),
            height=1, dont_extend_height=True,
            style="bg:#ansibrightblack fg:ansiwhite",
        )

        root = HSplit([
            header,
            output_window,
            separator,
            input_area,
            status_line,
        ])

        return Application(
            layout=Layout(root, focused_element=self._input_buffer),
            key_bindings=self._kb,
            full_screen=True,
            mouse_support=True,
        )

    @property
    def application(self):
        return self._app
