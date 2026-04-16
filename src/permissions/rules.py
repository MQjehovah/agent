import re
from dataclasses import dataclass, field
from pathlib import Path

from .modes import PermissionMode


@dataclass
class PathRule:
    pattern: str
    allow: bool
    _compiled: re.Pattern = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._compiled = re.compile(self._glob_to_regex(self.pattern))

    def matches(self, path: str) -> bool:
        return bool(self._compiled.match(str(Path(path).resolve())))

    @staticmethod
    def _glob_to_regex(pattern: str) -> str:
        result = pattern.replace(".", r"\.")
        result = result.replace("*", ".*")
        result = result.replace("?", ".")
        return f"^{result}$"


@dataclass
class PermissionConfig:
    mode: PermissionMode = PermissionMode.DEFAULT
    path_rules: list = field(default_factory=list)
    denied_commands: list = field(default_factory=lambda: [
        "rm -rf /",
        "rm -rf /*",
        "mkfs",
        "dd if=",
        ":(){ :|:& };:",
        "shutdown",
        "reboot",
        "format",
    ])
    write_tools: list = field(default_factory=lambda: [
        "file_operation",
        "shell",
        "edit",
    ])
    path_params: dict = field(default_factory=lambda: {
        "file_operation": "path",
        "edit": "path",
    })
