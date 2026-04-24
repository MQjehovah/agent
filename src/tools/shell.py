import asyncio
import json
import logging
import os
import re
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


def decode_output(data: bytes) -> str:
    if not data:
        return ""
    encodings = ["utf-8", "gbk", "cp936", "latin-1"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _close_transport(process: asyncio.subprocess.Process):
    """显式关闭子进程 pipe，避免 Event loop 关闭后 GC 触发 RuntimeError"""
    try:
        for pipe in (process.stdout, process.stderr, process.stdin):
            if pipe is not None:
                pipe.close()
    except Exception:
        pass


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

        try:
            env = {**os.environ, **extra_env} if extra_env else None

            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                _close_transport(process)
                logger.warning(f"命令执行超时: {command}")
                return json.dumps({
                    "success": False,
                    "error": f"命令执行超时（{timeout}秒）"
                }, ensure_ascii=False)

            stdout_str = decode_output(stdout)
            stderr_str = decode_output(stderr)
            _close_transport(process)

            # 按最大输出长度截断
            if len(stdout_str) > max_output:
                stdout_str = stdout_str[:max_output] + f"\n... [输出已截断，共 {len(stdout_str)} 字符]"
            if len(stderr_str) > min(max_output, 2000):
                stderr_str = stderr_str[:2000] + f"\n... [错误输出已截断]"

            result = {
                "success": process.returncode == 0,
                "return_code": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str
            }

            return json.dumps(result, ensure_ascii=False)

        except Exception as e:
            logger.error(f"命令执行失败: {e}")
            return json.dumps({
                "success": False,
                "error": str(e)
            }, ensure_ascii=False)
