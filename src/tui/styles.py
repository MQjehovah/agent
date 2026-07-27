import sys


def _sgr(c: str) -> str:
    return f"\033[{c}m" if sys.stdout.isatty() else ""


DIM = _sgr("2")
GREEN = _sgr("32")
YELLOW = _sgr("33")
CYAN = _sgr("36")
RED = _sgr("31")
RESET = _sgr("0")
BOLD = _sgr("1")
GRAY = _sgr("90")
ITALIC = _sgr("3")
