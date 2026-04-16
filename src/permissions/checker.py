import logging

from .modes import PermissionMode
from .rules import PermissionConfig

logger = logging.getLogger("agent.permissions")


class PermissionCheckResult:
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self):
        return self.allowed


class PermissionChecker:
    def __init__(self, config: PermissionConfig = None):
        self.config = config or PermissionConfig()

    def check(self, tool_name: str, arguments: dict) -> PermissionCheckResult:
        """检查工具调用是否被允许"""

        # AUTO 模式：全部放行
        if self.config.mode == PermissionMode.AUTO:
            return PermissionCheckResult(allowed=True)

        # PLAN 模式：禁止所有写操作
        if self.config.mode == PermissionMode.PLAN:
            if tool_name == "file_operation":
                op = arguments.get("operation", "")
                if op in ("read", "exists", "list"):
                    return PermissionCheckResult(allowed=True)
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"PLAN 模式禁止执行写操作: {tool_name}.{op}"
                )
            if tool_name in self.config.write_tools:
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"PLAN 模式禁止执行写操作工具: {tool_name}"
                )

        # 检查命令黑名单
        if tool_name == "shell":
            command = arguments.get("command", "")
            for denied in self.config.denied_commands:
                if denied in command:
                    return PermissionCheckResult(
                        allowed=False,
                        reason=f"危险命令被拦截: {denied}"
                    )

        # 检查路径规则
        path_param = self.config.path_params.get(tool_name)
        if path_param and path_param in arguments:
            path = arguments[path_param]
            for rule in self.config.path_rules:
                if rule.matches(path) and not rule.allow:
                    return PermissionCheckResult(
                        allowed=False,
                        reason=f"路径被规则拦截: {rule.pattern}"
                    )

        # DEFAULT 模式下写操作需要确认
        if self.config.mode == PermissionMode.DEFAULT:
            if tool_name in self.config.write_tools:
                # 判断是否为读操作
                if tool_name == "file_operation":
                    op = arguments.get("operation", "")
                    if op in ("read", "exists", "list"):
                        return PermissionCheckResult(allowed=True)
                elif tool_name == "shell":
                    command = arguments.get("command", "").strip()
                    # 纯读取类命令直接放行
                    read_prefixes = ("cat ", "head ", "tail ", "ls ", "find ",
                                     "grep ", "which ", "echo ", "type ", "pwd",
                                     "dir ", "more ", "less ", "stat ", "wc ")
                    for prefix in read_prefixes:
                        if command.startswith(prefix):
                            return PermissionCheckResult(allowed=True)
                return PermissionCheckResult(
                    allowed=True,
                    reason="需要用户确认"
                )

        return PermissionCheckResult(allowed=True)
