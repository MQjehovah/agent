import json
import sys

from .styles import BOLD, CYAN, DIM, GRAY, GREEN, RED, RESET, YELLOW


def _truncate(text: str, w: int = 60) -> str:
    if not text:
        return ""
    line = text.split("\n")[0].strip()
    if len(line) > w:
        return line[: w - 3] + "..."
    return line


def _fmt_args(args: dict) -> str:
    if not args:
        return ""
    for key in ("path", "file_path", "pattern", "name", "command"):
        val = args.get(key)
        if val:
            return str(val)[:60]
    return _truncate(json.dumps(args, ensure_ascii=False), 60)


def _write(text: str = "", end: str = "\n"):
    sys.stdout.write(text + end)
    sys.stdout.flush()


def clear_line():
    _write("\r\033[K", end="")


class Display:
    """Formats output and writes via a callback function."""

    def __init__(self, write_fn):
        self._write = write_fn

    def say(self, text: str):
        self._write(f"  {text}")

    def dim(self, text: str):
        self._write(f"  {DIM}{text}{RESET}")

    def ok(self, text: str):
        self._write(f"  {GREEN}{text}{RESET}")

    def warn(self, text: str):
        self._write(f"  {YELLOW}{text}{RESET}")

    def err(self, text: str):
        self._write(f"  {RED}{text}{RESET}")

    def rule(self):
        self._write(f"  {DIM}{'─' * 60}{RESET}")

    def tool_call(self, name: str, args: dict, prefix: str = ""):
        brief = _fmt_args(args)
        p = f"  {prefix}" if prefix else "  "
        self._write(f"{p}{DIM}● {name}{RESET} {GRAY}{brief}{RESET}")

    def tool_result(self, name: str, result: str):
        brief = _truncate(result, 60)
        if not brief or brief == "{}" or brief.startswith('{"success": true'):
            return
        if brief.startswith('{"success": false'):
            self._write(f"  {RED}✗ {name} → {brief[:80]}{RESET}")
            return
        self._write(f"  {GREEN}✔{RESET} {DIM}{brief}{RESET}")

    def subagent_result(self, name: str, status: str, preview: str = ""):
        s = f"{GREEN}done{RESET}" if status == "completed" else f"{RED}{status}{RESET}"
        line = f"  {DIM}└─ {name} [{s}]{RESET}"
        if preview:
            line += f"  {DIM}{preview}{RESET}"
        self._write(line)

    def user_message(self, text: str):
        if not text:
            return
        self._write(f"  {GREEN}{'━' * 10} 用户 {'━' * (53 - len(text.split(chr(10))[0]))}{RESET}")
        for line in text.strip().split("\n"):
            self._write(f"  {GREEN}❯{RESET} {line}")

    def assistant_message(self, agent_name: str, text: str):
        if not text:
            return
        label = agent_name or "助手"
        self._write(f"  {CYAN}{'━' * 10} {label} {'━' * (53 - len(label))}{RESET}")
        for line in text.strip().split("\n"):
            self._write(f"  {CYAN}│{RESET} {line}")

    def result_text(self, text: str, elapsed: str = ""):
        if not text:
            return
        self._write(f"  {GREEN}{'━' * 10} 完成 {'━' * 53}{RESET}")
        in_code = False
        for line in text.strip().split("\n"):
            if line.startswith("```"):
                if in_code:
                    self._write(f"  {DIM}└{'─' * 30}{RESET}")
                else:
                    code_lang = line[3:].strip()
                    self._write(f"  {DIM}┌{'─' * 30} {code_lang}{RESET}")
                in_code = not in_code
                continue
            if in_code:
                self._write(f"  {GRAY}│{RESET} {line}")
            elif line.strip().startswith("# ") or line.strip().startswith("## "):
                self._write(f"  {BOLD}{line}{RESET}")
            elif line.strip().startswith("- ") or line.strip().startswith("* "):
                self._write(f"  {DIM}•{RESET} {line.strip()[2:]}")
            elif line.strip():
                self._write(f"  {line}")
            else:
                self._write("")
        self._write(f"  {GREEN}{'━' * 10} 完成 {'━' * 53}{RESET}")
        footer = f"  {GREEN}completed{RESET}"
        if elapsed:
            footer += f" {DIM}in {elapsed}{RESET}"
        self._write(footer)

    def thinking(self, content: str):
        if not content:
            return
        for line in content.strip().split("\n")[-3:]:
            t = _truncate(line, 80)
            if t:
                self._write(f"  {DIM}┊ {t}{RESET}")

    def ask_question(self, question: str, options: list, default: str):
        self._write(f"  {BOLD}{'─' * 50}{RESET}")
        self._write(f"  {BOLD}{question}{RESET}")
        if options:
            for i, opt in enumerate(options, 1):
                self._write(f"  {DIM}{i}.{RESET} {opt}")
        if default:
            self._write(f"  {DIM}(default: {default}){RESET}")
        self._write(f"  {BOLD}{'─' * 50}{RESET}")

    def cancel_notice(self):
        self._write(f"  {YELLOW}已取消 (双击 ESC){RESET}")

    def error(self, msg: str):
        self._write(f"  {RED}错误: {msg}{RESET}")
