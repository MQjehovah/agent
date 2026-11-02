import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import shlex
import signal
import subprocess
import threading

from . import BuiltinTool

logger = logging.getLogger("agent.tools")

# 单条命令在内存里最多保留的输出（每个流），超出部分继续读取但丢弃。
# 历史事故：超时命令未杀 + 无上限 raw.read()，把 36GB 管道数据攒进父进程。
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def read_pipe_bounded(fh, limit_bytes: int = MAX_CAPTURE_BYTES) -> bytes:
    """读取管道直到 EOF，内存中最多保留 limit_bytes 字节。

    必须一直读到 EOF：提前停止读取会让子进程写满管道缓冲区后永久阻塞。
    """
    raw = getattr(fh, "buffer", fh)
    kept = bytearray()
    while True:
        chunk = raw.read(65536)
        if not chunk:
            break
        if len(kept) < limit_bytes:
            kept.extend(chunk[: limit_bytes - len(kept)])
    return bytes(kept)


def _new_process_group_kwargs() -> dict:
    """让子进程独立成组，超时时可以整组杀掉（含子孙进程）。"""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(proc) -> None:
    """杀掉子进程及其整个进程组；用于超时兜底，避免读取线程永久阻塞。"""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=5)


DENIED_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",
    "> /dev/sda",
]

INTERACTIVE_COMMANDS = {
    "passwd": "passwd 需要交互输入，禁止使用",
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
    if len(data) >= 2 and data[0:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode("utf-16")
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
        return ("在终端执行shell命令，返回命令输出结果。支持设置超时、工作目录和环境变量。\n"
                "仅用于没有专用工具的场景（如安装依赖、运行测试、系统命令等）。\n"
                "读文件用 file，编辑用 edit，搜索用 grep/glob，不要用 shell 替代。\n"
                "默认工作目录为临时目录，涉及项目文件的读写请使用 file 工具操作 workspace 目录。")

    @property
    def parameters(self) -> dict:
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
                "max_chars": {
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
        cwd = kwargs.get("cwd") or self.temp_dir or self.workspace or None
        extra_env = kwargs.get("env", {})
        max_output = kwargs.get("max_chars", 10000)

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

            # 将命令字符串拆分为参数列表（避免 shell=True 的安全风险）
            # 兼容 Windows 和 Unix 路径
            try:
                cmd_args = shlex.split(command, posix=(os.name != 'nt'))
                if not cmd_args:
                    cmd_args = [command]
            except Exception:
                cmd_args = command if isinstance(command, list) else [command]

            proc_holder: list = []

            def _run_sync():
                proc = subprocess.Popen(
                    cmd_args,
                    shell=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    **_new_process_group_kwargs(),
                )
                proc_holder.append(proc)

                stdout_result = [b""]
                stderr_result = [b""]

                def _read_pipe(fh, out):
                    try:
                        out[0] = read_pipe_bounded(fh, MAX_CAPTURE_BYTES)
                    finally:
                        with contextlib.suppress(Exception):
                            fh.close()

                t_out = threading.Thread(target=_read_pipe, args=(proc.stdout, stdout_result))
                t_err = threading.Thread(target=_read_pipe, args=(proc.stderr, stderr_result))
                t_out.start()
                t_err.start()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # 必须整组杀掉：否则子进程继续产出，读取线程永远不返回，
                    # 缓冲区会无限增长（线上 36GB 事故的根因）。
                    terminate_process_tree(proc)
                    raise
                finally:
                    t_out.join(timeout=5)
                    t_err.join(timeout=5)

                return subprocess.CompletedProcess(
                    args=command,
                    returncode=proc.returncode,
                    stdout=stdout_result[0],
                    stderr=stderr_result[0],
                )

            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, _run_sync),
                    timeout=timeout + 10
                )
            except (asyncio.TimeoutError, concurrent.futures.TimeoutError, subprocess.TimeoutExpired):
                logger.warning(f"命令执行超时 ({timeout}s): {command[:100]}")
                if proc_holder:
                    terminate_process_tree(proc_holder[0])
                return json.dumps({
                    "success": False,
                    "error": f"命令执行超时（{timeout}秒）"
                }, ensure_ascii=False)

            stdout_bytes = result.stdout or b""
            stderr_bytes = result.stderr or b""
            stdout_str = decode_output(stdout_bytes)
            stderr_str = decode_output(stderr_bytes)
            if stdout_str and stdout_str.count("\x00") > len(stdout_str) * 0.3:
                with contextlib.suppress(Exception):
                    stdout_str = stdout_bytes.decode("utf-16-le").rstrip("\x00").rstrip()
            if stderr_str and stderr_str.count("\x00") > len(stderr_str) * 0.3:
                with contextlib.suppress(Exception):
                    stderr_str = stderr_bytes.decode("utf-16-le").rstrip("\x00").rstrip()

            if len(stdout_str) > max_output:
                stdout_str = stdout_str[:max_output] + f"\n... [输出已截断，共 {len(stdout_str)} 字符]"
            if len(stderr_str) > min(max_output, 2000):
                stderr_str = stderr_str[:2000] + "\n... [错误输出已截断]"

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
