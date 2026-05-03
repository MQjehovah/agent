import logging
import re

logger = logging.getLogger("agent.sandbox")

DEFAULT_BLOCKED_PATTERNS = [
    (r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|.*--force\s+)/\s*$", "递归删除根目录"),
    (r"\bmkfs\b", "格式化磁盘"),
    (r"\bdd\s+if=", "dd 磁盘操作"),
    (r":\(\)\s*\{.*:\|:&\s*\}", "fork bomb"),
    (r">\s*/dev/sd[a-z]", "直接写磁盘设备"),
    (r"\b(shutdown|reboot|halt|poweroff|init\s+[06])\b", "系统关机命令"),
    (r"\b(sudo|su)\s+", "提权命令"),
    (r"\b(eval|exec)\s+.*\$[{(]", "通过变量执行任意命令"),
    (r"\bchmod\s+[0-7]*777\s+/", "递归修改根目录权限"),
    (r"\bcurl\b.*\|\s*(ba)?sh", "下载并执行远程脚本"),
    (r"\bwget\b.*\|\s*(ba)?sh", "下载并执行远程脚本"),
    (r"\bcrontab\b", "修改计划任务"),
    (r"\bsystemctl\b.*(start|stop|disable|enable|mask)", "系统服务管理"),
    (r"\b(iptables|firewall-cmd)\b", "防火墙修改"),
    (r"\bmount\b", "挂载文件系统"),
    (r"\bumount\b", "卸载文件系统"),
    (r"\bchown\b.*-R\s+/", "递归修改根目录所有者"),
    (r"\bservice\b\s+\w+\s+(start|stop|restart)", "系统服务管理"),
    (r"\bpasswd\b", "修改密码"),
    (r"\buser(add|del|mod)\b", "用户管理"),
    (r"\bgroup(add|del|mod)\b", "组管理"),
]

INTERACTIVE_COMMANDS = {
    "sudo": "sudo 需要密码交互，请使用 sudo -n (非交互模式) 或避免使用 sudo",
    "passwd": "passwd 需要交互输入，禁止使用",
    "ssh ": "ssh 可能要求密码/确认，禁止在 shell 工具中使用",
    "scp ": "scp 可能要求密码，禁止在 shell 工具中使用",
    "nano ": "nano 是交互式编辑器，禁止使用",
    "vim ": "vim 是交互式编辑器，禁止使用",
    "vi ": "vi 是交互式编辑器，禁止使用",
    "less ": "less 需要交互操作，请使用 cat 替代",
    "more ": "more 需要交互操作，请使用 cat 替代",
    "top ": "top 是交互式命令，禁止使用",
    "htop ": "htop 是交互式命令，禁止使用",
}


class CommandValidator:
    def __init__(self, extra_blocked_patterns: list[str] = None):
        self._patterns = []
        for pattern, reason in DEFAULT_BLOCKED_PATTERNS:
            try:
                self._patterns.append((re.compile(pattern, re.IGNORECASE), reason))
            except re.error as e:
                logger.warning(f"沙箱命令规则编译失败: {pattern}, 错误: {e}")

        if extra_blocked_patterns:
            for pattern in extra_blocked_patterns:
                try:
                    self._patterns.append((re.compile(pattern, re.IGNORECASE), f"自定义规则: {pattern}"))
                except re.error as e:
                    logger.warning(f"自定义命令规则编译失败: {pattern}, 错误: {e}")

    def validate(self, command: str) -> tuple[bool, str]:
        if not command or not command.strip():
            return False, "命令不能为空"

        for compiled, reason in self._patterns:
            if compiled.search(command):
                return False, f"危险命令被拦截: {reason}"

        cmd_lower = command.lower()
        for keyword, reason in INTERACTIVE_COMMANDS.items():
            if keyword.lower() in cmd_lower:
                return False, f"交互式命令被拦截: {reason}"

        return True, ""
