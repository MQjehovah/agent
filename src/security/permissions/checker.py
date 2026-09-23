import logging
from collections.abc import Callable
from pathlib import Path

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
    def __init__(self, config: PermissionConfig = None,
                 risk_resolver: Callable[[str], str | None] | None = None):
        self.config = config or PermissionConfig()
        # MCP 工具风险解析器(暴露工具名 → read/write/destructive/unknown);
        # 缺省 None 时行为与旧版完全一致(非 MCP 场景零影响)。
        self.risk_resolver = risk_resolver

    def _mcp_risk(self, tool_name: str) -> str | None:
        """解析 MCP 工具风险级别; 非 MCP/解析异常一律返回 None。"""
        if self.risk_resolver is None:
            return None
        try:
            risk = self.risk_resolver(tool_name)
        except Exception:
            return None
        return risk if risk in ("read", "write", "destructive", "unknown") else None

    def check(self, tool_name: str, arguments: dict) -> PermissionCheckResult:
        """检查工具调用是否被允许"""

        # AUTO 模式：全部放行
        if self.config.mode == PermissionMode.AUTO:
            # 即使 AUTO 模式，沙箱启用时也检查路径
            if self.config.sandbox_enabled:
                path_result = self._validate_file_path(tool_name, arguments)
                if not path_result.allowed:
                    return path_result
            return PermissionCheckResult(allowed=True)

        # MCP 工具级风险(注解 → 风险); 非 MCP 工具为 None, 不影响既有规则
        mcp_risk = self._mcp_risk(tool_name)

        # PLAN 模式：禁止所有写操作
        if self.config.mode == PermissionMode.PLAN:
            if tool_name == "file":
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
            if mcp_risk in ("write", "destructive"):
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"PLAN 模式禁止执行写类工具: {tool_name}"
                )

        # SMART 模式(必要时询问): 仅危险文件操作与高危命令需要确认, 其余写操作直接放行
        if self.config.mode == PermissionMode.SMART:
            if tool_name == "file":
                op = str(arguments.get("operation", "")).lower()
                if op in ("read", "exists", "list", "preview"):
                    return PermissionCheckResult(allowed=True)
                if op in self.config.dangerous_file_ops:
                    return PermissionCheckResult(allowed=True, reason="需要用户确认")
                # 写到工作区之外的文件也需要确认
                raw = str(arguments.get("path", "") or "")
                if raw and self.config.workspace_root:
                    try:
                        Path(raw).resolve().relative_to(Path(self.config.workspace_root).resolve())
                    except ValueError:
                        return PermissionCheckResult(allowed=True, reason="需要用户确认")
                    except OSError:
                        pass
                return PermissionCheckResult(allowed=True)
            if tool_name == "shell":
                command = str(arguments.get("command", "") or "").lower()
                for frag in self.config.dangerous_commands:
                    if frag in command:
                        return PermissionCheckResult(allowed=True, reason="需要用户确认")
                return PermissionCheckResult(allowed=True)
            if mcp_risk == "destructive":
                return PermissionCheckResult(allowed=True, reason="需要用户确认")
            return PermissionCheckResult(allowed=True)

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

        # 文件路径安全验证
        path_result = self._validate_file_path(tool_name, arguments)
        if not path_result.allowed:
            return path_result

        # DEFAULT 模式下写操作需要确认
        if self.config.mode == PermissionMode.DEFAULT and tool_name in self.config.write_tools:
            # 判断是否为读操作
            if tool_name == "file":
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

        # DEFAULT 模式下 MCP 写类工具与既有 write_tools 同语义: 需用户确认
        if self.config.mode == PermissionMode.DEFAULT and mcp_risk in ("write", "destructive"):
            return PermissionCheckResult(
                allowed=True,
                reason="需要用户确认"
            )

        return PermissionCheckResult(allowed=True)

    def _validate_file_path(self, tool_name: str, arguments: dict) -> PermissionCheckResult:
        """验证文件路径是否在允许/阻止范围内"""
        path_param = self.config.path_params.get(tool_name)
        if not path_param:
            return PermissionCheckResult(allowed=True)

        path = arguments.get(path_param, "")
        if not path:
            return PermissionCheckResult(allowed=True)

        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError) as e:
            return PermissionCheckResult(allowed=False, reason=f"路径解析失败: {e}")

        # 阻止路径穿越中的敏感路径
        for blocked in self.config.blocked_paths:
            try:
                resolved.relative_to(Path(blocked).resolve())
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"路径被禁止访问: {resolved}"
                )
            except ValueError:
                pass

        # 白名单模式（仅当配置了 allowed_paths 时生效）
        if self.config.allowed_paths:
            effective_allowed = list(self.config.allowed_paths)
            if self.config.workspace_root:
                effective_allowed.append(self.config.workspace_root)

            if effective_allowed and not any(
                str(resolved).startswith(str(Path(p).resolve()))
                for p in effective_allowed
            ):
                return PermissionCheckResult(
                    allowed=False,
                    reason=f"路径不在允许范围内: {resolved}"
                )

        return PermissionCheckResult(allowed=True)
