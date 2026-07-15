import re

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import to_filter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, WindowAlign
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout

_ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class ChatLayout:
    """Full-screen chat layout: output area | input line | status bar."""

    def __init__(self, status_bar):
        self.status_bar = status_bar
        self._submit_callback = None
        self._cancel_callback = None
        self._input_locked = False

        # Output buffer (plain text, ANSI-stripped) for proper scrolling
        self._output_text: list[str] = []
        self.output_buffer = Buffer(read_only=True)

        self.input_buffer = Buffer(
            multiline=False,
            completer=WordCompleter([]),
            complete_while_typing=False,
        )
        self._kb = self._build_key_bindings()
        self._app = self._build_application()

    def _build_key_bindings(self):
        kb = KeyBindings()

        @kb.add("c-c")
        def _exit(event):
            event.app.exit()

        @kb.add("c-d")
        def _eof(event):
            if not self.input_buffer.text:
                event.app.exit()

        @kb.add("escape", "escape")
        def _cancel(event):
            if self._cancel_callback:
                self._cancel_callback()

        @kb.add("enter")
        def _submit(event):
            if self._input_locked:
                return
            text = self.input_buffer.text
            if not text.strip():
                return
            self.input_buffer.text = ""
            if self._submit_callback:
                self._submit_callback(text)

        # Scrolling: output_buffer handles it automatically via Buffer's
        # built-in scroll support. No custom scroll binding needed.

        return kb

    @property
    def input_locked(self):
        return self._input_locked

    @input_locked.setter
    def input_locked(self, value: bool):
        self._input_locked = value
        self.input_buffer.read_only = to_filter(value)

    def on_submit(self, callback):
        self._submit_callback = callback

    def on_cancel(self, callback):
        self._cancel_callback = callback

    def _build_application(self):
        def _get_input_prefix():
            if self._input_locked:
                return HTML('<style fg="gray">⏳</style> ')
            return HTML('<style fg="green">❯</style> ')

        input_line = HSplit([
            Window(
                FormattedTextControl(
                    lambda: HTML(f'<style fg="gray">{"─" * 80}</style>'),
                ),
                height=1,
                align=WindowAlign.LEFT,
            ),
            VSplit([
                Window(
                    FormattedTextControl(_get_input_prefix),
                    width=4,
                    height=1,
                    dont_extend_width=True,
                ),
                Window(
                    BufferControl(
                        buffer=self.input_buffer,
                        focusable=True,
                    ),
                    height=1,
                    dont_extend_height=True,
                ),
            ]),
        ])

        status_line = Window(
            FormattedTextControl(self.status_bar.render),
            height=1,
            dont_extend_height=True,
            style="bg:#ansibrightblack fg:ansiwhite",
        )

        root = HSplit([
            Window(
                BufferControl(buffer=self.output_buffer, focusable=False),
                wrap_lines=True,
            ),
            input_line,
            status_line,
        ])

        return Application(
            layout=Layout(root, focused_element=self.input_buffer),
            key_bindings=self._kb,
            full_screen=True,
            mouse_support=False,
        )

    @property
    def application(self):
        return self._app

    def append_output(self, text: str = ""):
        plain = strip_ansi(text)
        self._output_text.append(plain)
        all_text = "\n".join(self._output_text)
        self.output_buffer.set_document(
            Document(all_text, len(all_text)), bypass_readonly=True)
        self._app.invalidate()

    def update_status(self):
        self._app.invalidate()
