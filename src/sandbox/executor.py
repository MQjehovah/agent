import asyncio
import concurrent.futures
import logging
import os
import shlex
import subprocess

logger = logging.getLogger("agent.sandbox")


class ProcessSandbox:
    def __init__(
        self,
        max_cpu_time: int = 60,
        max_memory_mb: int = 512,
        max_processes: int = 10,
        max_output_bytes: int = 10000,
    ):
        self.max_cpu_time = max_cpu_time
        self.max_memory_mb = max_memory_mb
        self.max_processes = max_processes
        self.max_output_bytes = max_output_bytes

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str = None,
        env: dict = None,
        max_output: int = None,
    ) -> dict:
        sandbox_env = {**os.environ}
        if env:
            dangerous_env = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH"}
            sandbox_env.update({
                k: v for k, v in env.items()
                if k not in dangerous_env
            })

        if any(k in command for k in ("apt-get ", "apt ", "aptitude ")):
            if "DEBIAN_FRONTEND" not in command:
                command = f"DEBIAN_FRONTEND=noninteractive {command}"
            if "-y" not in command.split():
                parts = command.split(" ", 1)
                command = f"{parts[0]} -y {parts[1]}" if len(parts) > 1 else command

        sandboxed_cmd = self._wrap_command(command)
        effective_timeout = min(timeout, self.max_cpu_time)

        try:
            def _run_sync():
                return subprocess.run(
                    sandboxed_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=cwd,
                    env=sandbox_env,
                    timeout=effective_timeout,
                )

            loop = asyncio.get_running_loop()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, _run_sync),
                    timeout=effective_timeout + 5,
                )
            except (asyncio.TimeoutError, concurrent.futures.TimeoutError, subprocess.TimeoutExpired):
                logger.warning(f"沙箱命令执行超时 ({effective_timeout}s): {command[:100]}")
                return {
                    "success": False,
                    "error": f"沙箱: 命令执行超时（{effective_timeout}秒）",
                    "sandbox": "process",
                }

            stdout = result.stdout or ""
            stderr = result.stderr or ""

            output_limit = max_output or self.max_output_bytes
            if len(stdout) > output_limit:
                stdout = stdout[:output_limit] + f"\n... [沙箱: 输出已截断，共 {len(stdout)} 字符]"
            if len(stderr) > 2000:
                stderr = stderr[:2000] + "\n... [沙箱: 错误输出已截断]"

            return {
                "success": result.returncode == 0,
                "return_code": result.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "sandbox": "process",
            }

        except Exception as e:
            logger.error(f"沙箱命令执行失败: {e}")
            return {"success": False, "error": f"沙箱执行失败: {e}", "sandbox": "process"}

    def _wrap_command(self, command: str) -> str:
        if os.name == "nt":
            return command

        wrappers = [
            f"ulimit -t {self.max_cpu_time}",
            f"ulimit -v {self.max_memory_mb * 1024}",
            f"ulimit -u {self.max_processes}",
        ]
        wrapped = shlex.quote(command)
        return f"bash -c '{'; '.join(wrappers)}; eval {wrapped}'"
