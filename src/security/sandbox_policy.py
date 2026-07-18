"""
沙箱安全策略分层系统

设计思路（参考 grok-build + TanStack AI 沙箱三层架构）：
- Provider 层：隔离级别（Docker/进程/VM）
- Workspace 层：文件系统视图（可见目录、读写权限、密钥注入）
- Policy 层：行为守卫（allow/ask/deny 三种模式，按操作类型配置）

用法:
    policy = SandboxPolicy()
    policy.configure({
        "provider": "docker",
        "workspace": {"read_only": ["/etc", "/usr"], "secrets": ["DB_PASSWORD"]},
        "policy": {"shell": "ask", "network": "deny", "file_write": "allow"},
    })

    # 执行检查
    result = policy.check("shell", {"command": "rm -rf /"})
    # result = {"action": "deny", "reason": "高风险命令"}
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("agent.sandbox_policy")


class Action(Enum):
    ALLOW = "allow"    # 允许
    ASK = "ask"        # 询问用户
    DENY = "deny"      # 拒绝


@dataclass
class PolicyRule:
    """策略规则"""
    action: Action
    reason: str = ""
    timeout: int = 0           # 此操作超时秒数
    allowed_args: list[str] = field(default_factory=list)  # 例外的参数模式


@dataclass
class SandboxProfile:
    """完整的沙箱配置"""
    name: str
    provider: str                    # docker / process / vm
    read_only_paths: list[str] = field(default_factory=list)
    invisible_paths: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    default_action: Action = Action.ASK
    rules: dict[str, PolicyRule] = field(default_factory=dict)


class SandboxPolicy:
    """沙箱安全策略引擎"""

    # 高风险命令模式（总是 deny）
    HIGH_RISK_PATTERNS = [
        r"rm\s+-rf\s+/",
        r"mkfs\.",
        r"dd\s+if=/.+of=/",
        r">\s*/dev/",
        r":\(\)\s*\{",
        r"wget\s+.*\|\s*bash",
        r"curl\s+.*\|\s*bash",
        r"chmod\s+777\s+/",
        r"sudo",
        r"passwd",
    ]

    def __init__(self):
        self.profiles: dict[str, SandboxProfile] = {}
        self._active_profile: Optional[SandboxProfile] = None
        self._init_defaults()

    def _init_defaults(self):
        """初始化默认配置"""
        self.profiles["default"] = SandboxProfile(
            name="default",
            provider="process",
            read_only_paths=["/etc", "/usr", "/opt"],
            invisible_paths=[],
            default_action=Action.ASK,
            rules={
                "shell": PolicyRule(Action.ASK, "Shell 命令需要确认"),
                "file_write": PolicyRule(Action.ALLOW, "允许写工作目录"),
                "file_read": PolicyRule(Action.ALLOW, "允许读文件"),
                "network": PolicyRule(Action.ASK, "网络请求需要确认"),
                "git": PolicyRule(Action.ALLOW, "Git 操作允许"),
            },
        )
        self.profiles["strict"] = SandboxProfile(
            name="strict",
            provider="docker",
            read_only_paths=["/etc", "/usr", "/opt", "/home"],
            invisible_paths=["/etc/shadow", "/etc/ssl", "/home/*/.ssh"],
            default_action=Action.DENY,
            rules={
                "shell": PolicyRule(Action.DENY, "严格模式下禁止 Shell 命令"),
                "file_write": PolicyRule(Action.ALLOW, "只允许写工作目录"),
                "file_read": PolicyRule(Action.ALLOW, "允许读文件"),
                "network": PolicyRule(Action.DENY, "严格模式下禁止网络请求"),
            },
        )
        self.profiles["dev"] = SandboxProfile(
            name="dev",
            provider="process",
            read_only_paths=[],
            default_action=Action.ALLOW,
            rules={
                "shell": PolicyRule(Action.ALLOW, "开发模式允许所有命令", timeout=120),
                "network": PolicyRule(Action.ALLOW, "允许网络"),
            },
        )

    def configure(self, config: dict):
        """从配置字典加载"""
        profile_name = config.get("profile", "default")
        if profile_name in self.profiles:
            self._active_profile = self.profiles[profile_name]
        else:
            # 自定义 profile
            profile = SandboxProfile(
                name=profile_name,
                provider=config.get("provider", "process"),
                read_only_paths=config.get("read_only_paths", []),
                invisible_paths=config.get("invisible_paths", []),
                secrets=config.get("secrets", []),
            )
            self.profiles[profile_name] = profile
            self._active_profile = profile
        logger.info(f"[sandbox_policy] 使用 profile: {profile_name}")

    def check(self, tool_name: str, args: dict) -> dict:
        """检查操作是否被允许

        Args:
            tool_name: 工具名
            args: 工具参数

        Returns:
            {"action": "allow|ask|deny", "reason": "原因"}
        """
        profile = self._active_profile or self.profiles["default"]

        # 高风险模式检查
        if tool_name == "shell":
            command = args.get("command", "")
            for pattern in self.HIGH_RISK_PATTERNS:
                if re.search(pattern, command):
                    return {
                        "action": "deny",
                        "reason": f"高风险命令被禁止: {command[:80]}",
                    }

        # 路径检查（file_write 时检查目标路径）
        if tool_name in ("file_operation", "batch_edit", "edit"):
            path = args.get("file", args.get("path", ""))
            if path:
                for rp in profile.read_only_paths:
                    if path.startswith(rp):
                        return {
                            "action": "deny",
                            "reason": f"目标路径 {path} 是只读的",
                        }
                for ip in profile.invisible_paths:
                    if path.startswith(ip) or (ip.endswith("*") and path.startswith(ip[:-1])):
                        return {
                            "action": "deny",
                            "reason": f"目标路径 {path} 不可见",
                        }

        # 策略规则检查
        category = self._categorize(tool_name)
        rule = profile.rules.get(category)
        if rule:
            # 检查参数白名单
            if rule.allowed_args:
                arg_str = json.dumps(args, ensure_ascii=False)
                if any(a in arg_str for a in rule.allowed_args):
                    return {"action": "allow", "reason": rule.reason}

            return {"action": rule.action.value, "reason": rule.reason, "timeout": rule.timeout}

        # 无匹配规则，用默认行为
        return {"action": profile.default_action.value, "reason": f"未配置 {tool_name} 的策略"}

    def check_shell(self, command: str) -> dict:
        """专门的 shell 安全检查"""
        return self.check("shell", {"command": command})

    def check_file_write(self, path: str) -> dict:
        """专门的文件写入检查"""
        return self.check("file_operation", {"path": path, "operation": "write"})

    def get_profile_info(self) -> dict:
        """获取当前配置信息"""
        profile = self._active_profile or self.profiles["default"]
        return {
            "name": profile.name,
            "provider": profile.provider,
            "default_action": profile.default_action.value,
            "read_only_paths": profile.read_only_paths,
            "invisible_paths": profile.invisible_paths,
            "rules_count": len(profile.rules),
        }

    def set_profile(self, name: str) -> bool:
        """切换 profile"""
        if name in self.profiles:
            self._active_profile = self.profiles[name]
            logger.info(f"[sandbox_policy] 切换到 profile: {name}")
            return True
        return False

    def _categorize(self, tool_name: str) -> str:
        """将工具名归类为策略类别"""
        category_map = {
            "shell": "shell",
            "file_operation": "file_write",
            "edit": "file_write",
            "batch_edit": "file_write",
            "write": "file_write",
            "web_search": "network",
            "web_fetch": "network",
            "code_search": "file_read",
            "grep": "file_read",
            "glob": "file_read",
            "read": "file_read",
            "git": "git",
        }
        return category_map.get(tool_name, "other")
