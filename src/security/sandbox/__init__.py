import json
import logging
import os

logger = logging.getLogger("agent.sandbox")


def load_sandbox_config(config_path: str = None) -> dict:
    if config_path and os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


class SandboxMiddleware:
    """沙箱中间层 — 独立于工具，由 Agent 执行链调用

    职责：
    1. 判断工具调用是否需要沙箱化
    2. 对 shell 类命令执行命令验证 + 路径验证 + 实际沙箱执行
    3. 对文件类工具执行路径验证（返回验证结果，不执行命令）
    4. 支持 per-tool 镜像覆盖
    """

    SANDBOXED_TOOLS = {"shell"}
    PATH_VALIDATED_TOOLS = {"file", "edit"}

    def __init__(self, config: dict, workspace: str = ""):
        from .command_validator import CommandValidator
        from .path_validator import PathValidator

        self.workspace = workspace
        self.config = config
        self.mode = config.get("mode", "process")

        # 验证器
        path_rules = config.get("path_rules", {})
        self.path_validator = PathValidator(
            allowed_paths=path_rules.get("allowed_paths", []),
            blocked_paths=path_rules.get("blocked_paths", []),
            workspace_root=workspace,
        )
        command_rules = config.get("command_rules", {})
        self.command_validator = CommandValidator(
            extra_blocked_patterns=command_rules.get("blocked_patterns", []),
        )

        # 镜像配置
        self._images = self._parse_images(config)
        self._tool_overrides = config.get("tool_overrides", {})

        # 资源限制
        self.resource_limits = config.get("resource_limits", {})

        # 执行器池（按 mode+image 缓存）
        self._executors: dict[str, object] = {}

    # ── 公开接口 ──────────────────────────────────────────

    def should_intercept(self, tool_name: str, args: dict) -> bool:
        """判断是否需要沙箱拦截此工具调用"""
        if tool_name in self.SANDBOXED_TOOLS:
            return True
        if tool_name in self.PATH_VALIDATED_TOOLS:
            path = args.get("path", "")
            if path:
                return True
        return False

    async def execute_shell(self, args: dict) -> dict | None:
        """拦截 shell 工具调用，返回执行结果或 None（放行由工具自行执行）"""
        command = args.get("command", "")
        timeout = args.get("timeout", 30)
        cwd = args.get("cwd")
        env = args.get("env", {})
        max_output = args.get("max_output", 10000)

        # 命令验证
        valid, reason = self.command_validator.validate(command)
        if not valid:
            return {"success": False, "error": reason, "sandbox": self.mode}

        # 工作目录验证
        cwd_path = cwd or self.workspace or os.getcwd()
        valid, reason = self.path_validator.validate_cwd(cwd_path)
        if not valid:
            return {"success": False, "error": f"工作目录不合法: {reason}", "sandbox": self.mode}

        # 选择执行器（支持 per-tool 镜像覆盖）
        image_override = self._tool_overrides.get("shell", {}).get("image")
        executor = self._get_executor(image=image_override)

        return await executor.execute(
            command=command,
            timeout=timeout,
            cwd=cwd,
            env=env,
            max_output=max_output,
        )

    def validate_path(self, path: str) -> tuple[bool, str]:
        """验证文件路径是否合法（供 file/edit 工具路径拦截）"""
        return self.path_validator.validate(path)

    # ── 镜像管理 ──────────────────────────────────────────

    def _parse_images(self, config: dict) -> dict[str, dict]:
        """解析多镜像配置"""
        images = {}

        # 默认镜像
        default = config.get("docker", {}).get("image", "sandbox-runner:latest")
        images["default"] = {
            "image": default,
            "memory_limit": config.get("docker", {}).get("memory_limit", "512m"),
            "cpu_period": config.get("docker", {}).get("cpu_period", 100000),
            "cpu_quota": config.get("docker", {}).get("cpu_quota", 60000),
            "pids_limit": config.get("docker", {}).get("pids_limit", 50),
            "network_mode": config.get("docker", {}).get("network_mode", "none"),
            "read_only_root": config.get("docker", {}).get("read_only_root", True),
        }

        # 自定义镜像列表
        for img in config.get("images", []):
            name = img.get("name", img.get("image", ""))
            images[name] = {
                "image": img.get("image", default),
                "memory_limit": img.get("memory_limit", images["default"]["memory_limit"]),
                "cpu_period": img.get("cpu_period", images["default"]["cpu_period"]),
                "cpu_quota": img.get("cpu_quota", images["default"]["cpu_quota"]),
                "pids_limit": img.get("pids_limit", images["default"]["pids_limit"]),
                "network_mode": img.get("network_mode", images["default"]["network_mode"]),
                "read_only_root": img.get("read_only_root", images["default"]["read_only_root"]),
            }

        return images

    def _get_executor(self, image: str = None):
        """获取执行器（按 mode+image 缓存）"""
        image_key = image or "default"
        cache_key = f"{self.mode}:{image_key}"

        if cache_key in self._executors:
            return self._executors[cache_key]

        if self.mode == "docker":
            try:
                executor = self._create_docker_executor(image_key)
                self._executors[cache_key] = executor
                return executor
            except Exception as e:
                logger.warning(f"Docker 沙箱不可用，回退到进程沙箱: {e}")

        executor = self._create_process_executor()
        self._executors[cache_key] = executor
        return executor

    def _create_docker_executor(self, image_key: str):
        from .docker_executor import DockerSandbox
        img_config = self._images.get(image_key, self._images["default"])
        return DockerSandbox(
            image=img_config["image"],
            memory_limit=img_config["memory_limit"],
            cpu_period=img_config["cpu_period"],
            cpu_quota=img_config["cpu_quota"],
            pids_limit=img_config["pids_limit"],
            network_mode=img_config["network_mode"],
            read_only_root=img_config["read_only_root"],
            workspace_mount=self.workspace or None,
            default_timeout=self.resource_limits.get("max_cpu_time", 60),
        )

    def _create_process_executor(self):
        from .executor import ProcessSandbox
        return ProcessSandbox(
            max_cpu_time=self.resource_limits.get("max_cpu_time", 60),
            max_memory_mb=self.resource_limits.get("max_memory_mb", 512),
            max_processes=self.resource_limits.get("max_processes", 10),
        )


def create_sandbox(config: dict, workspace: str = ""):
    """工厂函数：创建 SandboxMiddleware 或返回 None"""
    if not config or not config.get("enabled"):
        return None
    return SandboxMiddleware(config, workspace)
