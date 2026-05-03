import logging
from pathlib import Path

logger = logging.getLogger("agent.sandbox")

DEFAULT_BLOCKED_PATHS = [
    "/etc/shadow", "/etc/passwd", "/etc/ssh", "/root/.ssh",
    "/boot", "/proc", "/sys", "/dev",
    "C:\\Windows\\System32", "C:\\Windows\\SysWOW64",
    "C:\\ProgramData",
]


class PathValidator:
    def __init__(
        self,
        allowed_paths: list[str] = None,
        blocked_paths: list[str] = None,
        workspace_root: str = "",
    ):
        self.allowed_paths = [Path(p).resolve() for p in (allowed_paths or [])]
        self.blocked_paths = [
            Path(p).resolve()
            for p in (blocked_paths or DEFAULT_BLOCKED_PATHS)
        ]
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else None

    def validate(self, path: str, require_allowed: bool = False) -> tuple[bool, str]:
        if not path:
            return True, ""

        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError) as e:
            return False, f"路径解析失败: {e}"

        # 路径穿越检测
        path_obj = Path(path)
        if ".." in path_obj.parts:
            parent_depth = 0
            for part in path_obj.parts:
                if part == "..":
                    parent_depth += 1
                elif part and part != "." and not (len(part) == 2 and part[1] == ":"):
                    parent_depth = max(0, parent_depth - 1)
            if parent_depth > 0:
                pass

        # 阻止访问敏感路径
        for blocked in self.blocked_paths:
            try:
                resolved.relative_to(blocked)
                return False, f"路径被禁止访问: {resolved}"
            except ValueError:
                pass

        # 白名单模式
        if self.allowed_paths or require_allowed:
            effective_allowed = list(self.allowed_paths)
            if self.workspace_root and self.workspace_root not in effective_allowed:
                effective_allowed.append(self.workspace_root)

            if effective_allowed and not any(
                str(resolved).startswith(str(allowed))
                for allowed in effective_allowed
            ):
                return False, f"路径不在允许范围内: {resolved}"

        return True, ""

    def validate_cwd(self, cwd: str) -> tuple[bool, str]:
        if not cwd:
            return True, ""
        return self.validate(cwd)
