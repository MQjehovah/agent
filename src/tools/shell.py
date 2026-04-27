import asyncio
import json
import logging
import os
import subprocess
import concurrent.futures
from typing import Dict, Any

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

# 危险命令黑名单
DENIED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",
    "> /dev/sda",
]

# 可能在 shell 中要求交互式输入的命令（会永久挂起）
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
}


def decode_output(data: bytes | str) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return data
    encodings = ["utf-8", "gbk", "cp936", "latin-1"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class ShellTool(BuiltinTool):
    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return "在终端执行shell命令，返回命令输出结果。支持设置超时、工作目录和环境变量。"

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的shell命令"
                },
                "timeout": {
                    "type": "integer",
                    "description": "命令执行超时时间（秒），默认30秒",
                    "default": 30
                },
                "cwd": {
                    "type": "string",
                    "description": "命令执行的工作目录，默认当前目录"
                },
                "env": {
                    "type": "object",
                    "description": "环境变量，如 {'DEBUG': '1'}",
                    "default": {}
                },
                "max_output": {
                    "type": "integer",
                    "description": "输出最大字符数，默认10000",
                    "default": 10000
                }
            },
            "required": ["command"]
        }

    async def execute(self, **kwargs) -> str:
        command = kwargs.get("command", "")
        timeout = kwargs.get("timeout", 30)
        cwd = kwargs.get("cwd")
        extra_env = kwargs.get("env", {})
        max_output = kwargs.get("max_output", 10000)

        if not command:
            return json.dumps({"success": False, "error": "命令不能为空"}, ensure_ascii=False)

        # 危险命令检查
        for denied in DENIED_COMMANDS:
            if denied in command:
                return json.dumps({
                    "success": False,
                    "error": f"危险命令被拦截: {denied}"
                }, ensure_ascii=False)

        # 交互式命令检查（会永久挂起）
        cmd_lower = command.lower()
        for keyword, reason in INTERACTIVE_COMMANDS.items():
            if keyword.lower() in cmd_lower:
                return json.dumps({
                    "success": False,
                    "error": f"交互式命令被拦截: {reason}"
                }, ensure_ascii=False)

        # 自动给 apt/apt-get 加非交互标志
        if any(k in command for k in ("apt-get ", "apt ", "aptitude ")):
            if "DEBIAN_FRONTEND" not in command:
                command = f"DEBIAN_FRONTEND=noninteractive {command}"
            if "-y" not in command.split():
                parts = command.split(" ", 1)
                command = f"{parts[0]} -y {parts[1]}" if len(parts) > 1 else command

        try:
            env = {**os.environ, **extra_env} if extra_env else None

            def _run_sync():
                return subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    cwd=cwd,
                    env=env,
                    timeout=timeout,
                )

            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, _run_sync),
                    timeout=timeout + 5
                )
            except (asyncio.TimeoutError, concurrent.futures.TimeoutError, subprocess.TimeoutExpired):
                logger.warning(f"命令执行超时 ({timeout}s): {command[:100]}")
                return json.dumps({
                    "success": False,
                    "error": f"命令执行超时（{timeout}秒）"
                }, ensure_ascii=False)

            stdout_str = decode_output(result.stdout)
            stderr_str = decode_output(result.stderr)

            # 按最大输出长度截断
            if len(stdout_str) > max_output:
                stdout_str = stdout_str[:max_output] + f"\n... [输出已截断，共 {len(stdout_str)} 字符]"
            if len(stderr_str) > min(max_output, 2000):
                stderr_str = stderr_str[:2000] + f"\n... [错误输出已截断]"

            result_json = {
                "success": result.returncode == 0,
                "return_code": result.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str
            }

            return json.dumps(result_json, ensure_ascii=False)

        except Exception as e:
            logger.error(f"命令执行失败: {e}")
            return json.dumps({
                "success": False,
                "error": str(e)
            }, ensure_ascii=False)
