from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings


class AskHandler:
    """Handles ask_user interactions using a dedicated PromptSession."""

    def __init__(self, status_bar):
        self._status_bar = status_bar
        self._kb = KeyBindings()

        @self._kb.add("c-c")
        def _exit(event):
            raise KeyboardInterrupt

        @self._kb.add("c-d")
        def _eof(event):
            raise EOFError

        self._session = PromptSession(
            history=InMemoryHistory(),
            key_bindings=self._kb,
            multiline=False,
        )

    async def ask(self, question: str, options: list, default: str) -> str:
        if options:
            items = [f"  {i}. {opt}" for i, opt in enumerate(options, 1)]
            "\n" + "\n".join(items)

        msg = HTML('<style fg="cyan">❯</style> ')
        hint = ""
        if options:
            hint = f" (1-{len(options)}"
            hint += f", default: {default}" if default else ""
            hint += ")"
        elif default:
            hint = f" (default: {default})"

        self._status_bar.set_waiting(question)
        try:
            answer = await self._session.prompt_async(
                message=msg,
                bottom_toolbar=self._status_bar.render,
            )
            ans = answer.strip()
            if not ans:
                return default or ""
            if options and ans.isdigit():
                idx = int(ans) - 1
                if 0 <= idx < len(options):
                    return options[idx]
            return ans
        finally:
            pass

    def ask_sync(self, question: str, options: list, default: str) -> str:
        """Synchronous fallback (not ideal, but used when no event loop)."""
        print(f"\n{question}")
        if options:
            for i, opt in enumerate(options, 1):
                print(f"  {i}. {opt}")
        prompt_str = "❯ "
        if default:
            prompt_str += f"(default: {default}) "
        raw = input(prompt_str).strip()
        if not raw:
            return default or ""
        if options and raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        return raw
