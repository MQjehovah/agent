import asyncio
import contextlib
import logging

logger = logging.getLogger("agent.sandbox")


class DockerSandbox:
    def __init__(
        self,
        image: str = "sandbox-runner:latest",
        memory_limit: str = "512m",
        cpu_period: int = 100000,
        cpu_quota: int = 60000,
        pids_limit: int = 50,
        network_mode: str = "none",
        read_only_root: bool = True,
        workspace_mount: str = None,
        default_timeout: int = 60,
    ):
        self.image = image
        self.memory_limit = memory_limit
        self.cpu_period = cpu_period
        self.cpu_quota = cpu_quota
        self.pids_limit = pids_limit
        self.network_mode = network_mode
        self.read_only_root = read_only_root
        self.workspace_mount = workspace_mount
        self.default_timeout = default_timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            import docker
            self._client = docker.from_env()
        return self._client

    async def execute(
        self,
        command: str,
        timeout: int = None,
        cwd: str = "/workspace",
        env: dict = None,
        max_output: int = 10000,
    ) -> dict:
        actual_timeout = timeout or self.default_timeout

        volumes = {}
        if self.workspace_mount:
            volumes[self.workspace_mount] = {"bind": "/workspace", "mode": "rw"}

        container_env = env or {}

        try:
            client = self._get_client()

            def _run_container():
                tmpfs = {}
                if self.read_only_root:
                    tmpfs["/tmp"] = "size=100m"
                    tmpfs["/run"] = "size=10m"
                    tmpfs["/home"] = "size=100m"

                container = client.containers.run(
                    self.image,
                    command=["/bin/sh", "-c", command],
                    detach=True,
                    mem_limit=self.memory_limit,
                    memswap_limit=self.memory_limit,
                    cpu_period=self.cpu_period,
                    cpu_quota=self.cpu_quota,
                    pids_limit=self.pids_limit,
                    network_mode=self.network_mode,
                    read_only=self.read_only_root,
                    tmpfs=tmpfs if tmpfs else None,
                    volumes=volumes if volumes else None,
                    environment=container_env,
                    working_dir=cwd or "/workspace",
                    stdout=True,
                    stderr=True,
                )

                try:
                    result = container.wait(timeout=actual_timeout)
                    exit_code = result.get("StatusCode", -1)
                    stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
                    stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
                    return exit_code, stdout, stderr
                finally:
                    with contextlib.suppress(Exception):
                        container.remove(force=True)

            loop = asyncio.get_running_loop()
            try:
                exit_code, stdout, stderr = await asyncio.wait_for(
                    loop.run_in_executor(None, _run_container),
                    timeout=actual_timeout + 30,
                )
            except asyncio.TimeoutError:
                return {
                    "success": False,
                    "error": f"沙箱容器执行超时（{actual_timeout}秒）",
                    "sandbox": "docker",
                }

            if len(stdout) > max_output:
                stdout = stdout[:max_output] + "\n... [容器沙箱: 输出已截断]"
            if len(stderr) > 2000:
                stderr = stderr[:2000] + "\n... [容器沙箱: 错误输出已截断]"

            return {
                "success": exit_code == 0,
                "return_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "sandbox": "docker",
            }

        except ImportError:
            return {"success": False, "error": "docker 包未安装，请运行: pip install docker", "sandbox": "docker"}
        except Exception as e:
            error_msg = str(e)
            if "image" in error_msg.lower() and ("not found" in error_msg.lower() or "no such" in error_msg.lower()):
                return {
                    "success": False,
                    "error": f"沙箱镜像不存在: {self.image}，请先构建: docker build -t {self.image} sandbox/",
                    "sandbox": "docker",
                }
            logger.error(f"Docker 沙箱错误: {e}")
            return {"success": False, "error": f"Docker 沙箱不可用: {e}", "sandbox": "docker"}
