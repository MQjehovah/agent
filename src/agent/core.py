import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import time
import uuid
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from openai.types.chat import ChatCompletionMessageParam

from agent.context import AgentResult, RunContext, current_run, _current_run
from learning import Learner
from conversation.prompt import PromptBuilder
from agent.factory import AgentFactory
from utils.frontmatter import extract_frontmatter

if TYPE_CHECKING:
    from plugins import PluginManager

logger = logging.getLogger("agent.agent")


MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("MAX_TOOL_OUTPUT_CHARS", 3000))


class Agent:
    def __init__(
        self,
        workspace: str,
        client,
        parent_agent: "Agent" = None,
        permission_mode: str = "auto",
        config_dir: str = "",
        mcp_servers: list = None,
    ):
        self.workspace = workspace
        self.client = client
        self.parent_agent = parent_agent
        self.config_dir = config_dir or workspace

        self.agent_id = ""
        self.name = ""
        self.description = ""
        self.system_prompt = ""
        self.system_prompt_raw = ""
        # P3: prompt cache 拆分（实例级初始值；run 内由 RunContext 持有）
        self.system_static = ""
        self.system_dynamic = ""
        self.max_iterations = 200
        self.tool_denylist: set[str] = set()

        # Prompt 分层拼装器
        self._prompt_builder: PromptBuilder | None = None

        # 压缩后状态恢复：最近读取的文件
        self._recent_files: OrderedDict[str, str] = OrderedDict()
        self._max_recent_files = 5

        self.tool_registry = None
        self.mcp = None
        # 子代理专属 MCP 配置（运行时传入，方案B；主代理始终为空）
        self._subagent_mcp_configs = list(mcp_servers) if mcp_servers else []
        self.skill_manager = None
        self.factory = None
        # 向后兼容：subagent_manager -> factory
        self.subagent_manager = None
        self.session_manager = None
        self.storage = None
        self.plugin_manager: PluginManager | None = None
        self.memory = None
        self.learner: Learner | None = None
        self._background_tasks: set = set()

        self.status = "pending"
        self.result: str | None = None

        # 临时工作目录（系统 temp 下，按 session 隔离，cleanup 时自动删除）
        self.temp_dir: str = "tmp"

        # 沙箱系统
        self.sandbox = None

        # 权限系统
        from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
        self._permission_config = PermissionConfig(
            mode=PermissionMode(permission_mode)
        )
        self.permission = PermissionChecker(self._permission_config)

        # 钩子系统
        from hooks import HookEvent, HookManager
        self.hooks = HookManager()
        self._hook_event = HookEvent

        # 调用链路追踪
        from llm.tracing import Tracer
        self.tracer = Tracer()

        # 后台任务管理器
        from tools.task import TaskManager
        self.task_manager = TaskManager()

        # 用户确认回调（外部注入，如交互模式中的 input()）
        self.on_confirm = None

        self._env_context_cache: str = ""
        self._env_context_time: float = 0.0

        self.rbac = None

        from settings import get_settings
        self._learning_enabled = get_settings().get("learning.enabled", False)
        self._learning_per_round = get_settings().get("learning.per_round", False)
        self._learning_auto_create = get_settings().get("learning.auto_create", False)
        self._is_team = False
        self._team_config: dict = {}
        self._team_members: dict = {}

        # ── v2.0: Plan Mode ──
        self._plan_mode = None
        self._enable_plan_mode = True
        self._plan_mode_config = {
            "auto_plan": True,               # 自动判断是否进入 Plan Mode
            "require_approval": True,         # 是否需要用户审批
        }

        # ── 循环控制（外部注入，如 TUI 的取消信号）──
        self._shutdown_event = None
        self._cancel_flag = None

        # ── Agent 循环模式: "react"(标准ReAct) / "reflective"(计划→执行→评估→调整) ──
        self.loop_mode = "react"

        # ── v2.0: Agent 连接池（团队模式下使用） ──
        self._agent_pool = None

        # ── v2.0: 并行的 TeamOrchestrator ──
        self._enable_parallel = True
        self._max_parallel = 4

    async def initialize(self, session_id: str = None):
        self._load_system_prompt()
        await self._load_team_config()
        self._init_sandbox()
        self._create_temp_dir()
        self._init_tools()
        self.tool_registry.temp_dir = self.temp_dir
        self._init_skills()
        await self._load_mcp_servers()

        from conversation.session import AgentSessionManager
        from storage.storage import init_storage
        self.session_manager = AgentSessionManager()
        await self.session_manager.start_cleanup_task()

        if self.parent_agent and self.parent_agent.storage:
            self.storage = self.parent_agent.storage
        else:
            self.storage = init_storage(self.workspace, config_dir=self.config_dir)

        from security.rbac import RBACManager
        self.rbac = RBACManager(self.storage)

        self._init_factory()
        self._init_memory()

        # 将 memory_manager 注入已注册的 MemoryTool
        memory_tool = self.tool_registry.get_tool("memory")
        if memory_tool and self.memory:
            memory_tool.set_memory_manager(self.memory)

        # 构建分层 prompt（必须在所有初始化完成后）
        self._build_prompt()

    def _load_system_prompt(self):
        prompt_file = os.path.join(self.config_dir, "PROMPT.md")

        if not os.path.exists(prompt_file):
            logger.warning(f"No PROMPT.md found in {self.config_dir}")
            self.agent_id = self.name = ""
            return

        with open(prompt_file, encoding="utf-8") as f:
            content = f.read()

        frontmatter, body = extract_frontmatter(content)

        if frontmatter:
            self.agent_id = self.name = frontmatter.get("name", "")
            self.description = frontmatter.get("description", "")
            if isinstance(self.description, str):
                self.description = self.description.strip()

        self.system_prompt = self._expand_env_vars(body.strip()) if body else ""
        self.system_prompt_raw = self.system_prompt

    async def _load_team_config(self):
        """检查 config_dir 下是否有 TEAM.md，加载团队配置"""
        team_file = os.path.join(self.config_dir, "TEAM.md")
        if not os.path.exists(team_file):
            return
        from utils.frontmatter import extract_frontmatter
        with open(team_file, encoding="utf-8") as f:
            content = f.read()
        fm, body = extract_frontmatter(content)
        if not fm:
            return
        name = fm.get("name", os.path.basename(self.config_dir))
        raw_members = fm.get("members", [])
        team_roles = ""
        if isinstance(raw_members, list):
            lines = []
            for m in raw_members:
                if isinstance(m, dict):
                    lines.append(f"- {m.get('name', '')}: {m.get('role', '')}")
                else:
                    lines.append(f"- {m}")
            team_roles = "\n".join(lines)
        # 构建团队配置（与 SubagentManager._load_team 格式一致）
        # 加载团队 Leader prompt（从团队目录下的 PROMPT.md 读取）
        team_prompt_file = os.path.join(self.config_dir, "PROMPT.md")
        leader_prompt = self.system_prompt_raw or ""
        if os.path.exists(team_prompt_file):
            try:
                with open(team_prompt_file, encoding="utf-8") as _f:
                    _team_content = _f.read()
                from utils.frontmatter import extract_frontmatter as _ef
                _fm, _body = _ef(_team_content)
                if _body:
                    leader_prompt = _body
            except Exception as _e:
                logger.debug(f"读取团队 PROMPT.md 失败: {_e}")

        self._team_config = {
            "name": name,
            "description": fm.get("description", ""),
            "leader": fm.get("leader", ""),
            "pipeline_mode": fm.get("pipeline_mode", "auto"),
            "workspace": self.workspace,
            "team_body": body,
            "team_roles": team_roles,
            "leader_prompt": leader_prompt,
            "dir_name": name,
        }
        # 加载成员
        agents_dir = os.path.join(self.config_dir, "agents")
        members = {}
        if os.path.exists(agents_dir):
            from utils.frontmatter import extract_frontmatter as _ef
            for mname in os.listdir(agents_dir):
                mp = os.path.join(agents_dir, mname)
                if not os.path.isdir(mp):
                    continue
                pf = os.path.join(mp, "PROMPT.md")
                if not os.path.exists(pf):
                    continue
                with open(pf, encoding="utf-8") as _f:
                    _fm, _ = _ef(_f.read())
                members[mname] = {
                    "name": _fm.get("name", mname) if _fm else mname,
                    "description": _fm.get("description", "") if _fm else "",
                    "workspace": self.workspace,
                    "config_dir": mp,
                }
        self._team_members = members
        self._is_team = True
        logger.info(f"Agent [{self.name}] 检测到 TEAM.md，加载团队: {name}, 成员: {list(members.keys())}")

    def _init_sandbox(self):
        """加载并初始化沙箱中间层"""
        sandbox_config_path = os.path.join(self.config_dir, "sandbox.json")
        try:
            from security.sandbox import create_sandbox, load_sandbox_config
            config = load_sandbox_config(sandbox_config_path)
            if config:
                self.sandbox = create_sandbox(config, self.workspace)
                if self.sandbox:
                    self._permission_config.sandbox_enabled = True
                    pv = self.sandbox.path_validator
                    self._permission_config.blocked_paths = [
                        str(p) for p in pv.blocked_paths
                    ]
                    self._permission_config.allowed_paths = [
                        str(p) for p in pv.allowed_paths
                    ]
                    self._permission_config.workspace_root = self.workspace
                    self.permission = type(self.permission)(self._permission_config)
                    logger.info(
                        f"Agent [{self.name}] 沙箱已启用: {type(self.sandbox).__name__}")
                else:
                    logger.debug(f"Agent [{self.name}] 沙箱未启用 (config.enabled=false)")
        except Exception as e:
            logger.warning(f"Agent [{self.name}] 沙箱初始化失败: {e}")
            self.sandbox = None

    def _create_temp_dir(self):
        import tempfile
        suffix = f"_{self.name}" if self.name else ""
        self.temp_dir = tempfile.mkdtemp(suffix=suffix, prefix="agent_")
        logger.info(f"Agent [{self.name}] 临时目录: {self.temp_dir}")

    @staticmethod
    def _expand_env_vars(text: str) -> str:
        """替换 ${VAR:default} 或 ${VAR} 为环境变量值"""
        def _replace(match):
            full = match.group(1).strip()
            if ":" in full:
                var, default = full.split(":", 1)
                return os.environ.get(var, default)
            return os.environ.get(full, "")
        return re.sub(r'\$\{([^}]+)\}', _replace, text)

    def _init_tools(self):
        from tools import ToolRegistry

        self.tool_registry = ToolRegistry()
        self.tool_registry.workspace = self.workspace

        self.tool_registry.auto_discover()

        # task/bind_session 是管理命令，不是 LLM 工具，排除
        self.tool_denylist.update(["task_list", "task_get", "task_create", "task_cancel",
                                   "bind_session"])

        self._init_retrieval()

        self._init_code_quality()

        logger.info(
            f"Agent [{self.name}] 已注册 {len(self.tool_registry.list_tools())} 个工具: {self.tool_registry.list_tools()}"
            + (f" [沙箱: {type(self.sandbox).__name__}]" if self.sandbox else " [沙箱: 未启用]"))

    def _init_retrieval(self):
        from settings import get_settings
        settings = get_settings()
        rag_url = settings.env_str("rag.base_url", "RAG_BASE_URL", "")
        if not rag_url:
            return

        from retrieval import RetrievalTool
        tool = RetrievalTool()
        tool.configure(
            base_url=rag_url,
            username=settings.env_str("rag.username", "RAG_USERNAME", ""),
            password=settings.env_str("rag.password", "RAG_PASSWORD", ""),
            token=settings.env_str("rag.token", "RAG_TOKEN", ""),
        )
        self.tool_registry.register_tool(tool)
        logger.info(f"Agent [{self.name}] RAG 知识库已接入: {rag_url}")

    def _init_code_quality(self):
        """初始化代码质量相关模块"""
        try:
            # ── .agentignore ──
            from agent.ignore import AgentIgnore
            self._agent_ignore = AgentIgnore(self.workspace)
            self._agent_ignore.inject_into(self.tool_registry)

            # 生成示例文件（如果不存在）
            agent_dir = os.path.join(self.workspace, ".agent")
            if not os.path.exists(os.path.join(agent_dir, ".agentignore")):
                self._agent_ignore.generate_example(self.workspace)
        except Exception as e:
            logger.warning(f"初始化 .agentignore 失败: {e}")
            self._agent_ignore = None

        try:
            # ── 熔断器 ──
            from quality.circuit_breaker import CircuitBreaker
            self._circuit_breaker = CircuitBreaker(
                name=f"agent:{self.name}" if self.name else "agent",
                threshold=5,
                cooldown=30,
            )
        except Exception as e:
            logger.warning(f"初始化熔断器失败: {e}")
            self._circuit_breaker = None

        try:
            # ── Git 集成 ──
            from worker.git_integration import GitIntegration
            self._git = GitIntegration(self.workspace)
        except Exception as e:
            logger.warning(f"初始化 Git 集成失败: {e}")
            self._git = None

        try:
            # ── 代码质量钩子 ──
            from quality.quality_hooks import CodeQualityHooks
            self._quality_hooks = CodeQualityHooks(self.workspace)
            if hasattr(self, 'hooks') and self.hooks:
                self._quality_hooks.register_all(self.hooks)
        except Exception as e:
            logger.warning(f"初始化质量钩子失败: {e}")
            self._quality_hooks = None

        try:
            # ── 自动技能路由 ──
            from worker.auto_skill import AutoSkillActivator
            self._auto_skill = AutoSkillActivator(self.skill_manager)
        except Exception as e:
            logger.warning(f"初始化自动技能路由失败: {e}")
            self._auto_skill = None

        logger.info(f"Agent [{self.name}] 代码质量模块初始化完成")

    def _init_skills(self):
        skills_dir = os.path.join(self.config_dir, "skills")
        if os.path.exists(skills_dir):
            from skills import SkillManager
            self.skill_manager = SkillManager(skills_dir)
            logger.info(
                f"Agent [{self.name}] 已加载 {len(self.skill_manager.list_skills())} 个技能: {[self.skill_manager.list_skills()]}")

    async def _load_mcp_servers(self):
        if self.parent_agent:
            # 子 agent（方案B）：优先运行时传入的 mcp_servers，否则读自己 config_dir 的 mcp_servers.json
            self.mcp_configs = list(self._subagent_mcp_configs) or self._read_mcp_config_file()
            if not self.mcp_configs:
                return
            await self._connect_mcp_servers(subagent=True)
            return

        # 主 agent：读 config_dir/mcp_servers.json
        self.mcp_configs = self._read_mcp_config_file()
        if self.mcp_configs:
            await self._connect_mcp_servers(subagent=False)

    def _read_mcp_config_file(self) -> list:
        """读取 config_dir/mcp_servers.json（主子代理共用）"""
        mcp_file = os.path.join(self.config_dir, "mcp_servers.json")
        if not os.path.exists(mcp_file):
            return []
        try:
            with open(mcp_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load mcp_servers.json: {e}")
            return []

    async def _connect_mcp_servers(self, subagent: bool = False):
        """连接 mcp_configs 中的 MCP servers"""
        from mcps import MCPManager
        self.mcp = MCPManager("")
        for config in self.mcp_configs:
            if config.get("enabled", True):
                try:
                    await self.mcp.connect_server(config)
                except Exception as e:
                    logger.warning(f"MCP server [{config.get('name', 'unnamed')}] 连接失败: {e}")
                    continue
            else:
                logger.debug(
                    f"跳过已禁用的 MCP server: {config.get('name', 'unnamed')}")
        self.mcp.start_health_check()

        connected = [c.get("name", "unnamed")
                     for c in self.mcp_configs if c.get("enabled", True)]
        role = "子 Agent" if subagent else "Agent"
        logger.info(
            f"{role} [{self.name}] 已连接 {len(connected)} MCP servers: {connected}")

    def _init_factory(self):
        self.factory = AgentFactory(self.config_dir, self.workspace)
        self.factory._parent_agent = self
        self.factory.start_cleanup_task()
        self.subagent_manager = self.factory  # 向后兼容
        logger.info(
            f"Agent [{self.name}] 已加载 {len(self.factory.scan_teams())} 个模板: {self.factory.scan_teams()}")

    def _init_memory(self):
        from memory import MemoryManager
        if self.parent_agent and self.parent_agent.memory:
            self.memory = self.parent_agent.memory
        else:
            from storage.storage import get_storage
            self.memory = MemoryManager(storage=get_storage(), agent_id=self.agent_id)

        if self._learning_enabled:
            self.learner = Learner(
                memory_manager=self.memory,
                llm_client=self.client,
                agent_id=self.agent_id,
            )

            if not self.parent_agent:
                if self._learning_auto_create:
                    self.learner.init_auto_creation(
                        workspace=self.config_dir,
                        skill_manager=self.skill_manager,
                        factory=self.factory,
                    )
                from memory.curator import MemoryCurator
                from storage.storage import get_storage
                self.curator = MemoryCurator(storage=get_storage(), llm_client=self.client)
                self.learner.set_curator(self.curator)
                self.learner.start_daily_task()
        else:
            self.learner = None

        memory_tool = self.tool_registry.get_tool("memory")
        if memory_tool and hasattr(memory_tool, 'set_memory_manager'):
            memory_tool.set_memory_manager(self.memory)

    # ------------------------------------------------------------------ #
    #  Prompt 分层拼装
    # ------------------------------------------------------------------ #

    def _build_prompt(self, task: str = ""):
        """使用 PromptBuilder 构建分层 prompt（写入当前 run 上下文；initialize 时写实例）"""
        rc = _current_run.get()
        builder = PromptBuilder()

        # === 静态区 (可被 LLM prompt cache 缓存) ===
        builder.add(
            "角色定义", self.system_prompt_raw,
            is_static=True, priority=0
        )

        # === 动态区 (每轮可能变化) ===
        builder.add(
            "环境上下文", self._get_env_context(),
            is_static=False, priority=30
        )

        # 子代理列表
        if self.factory:
            subagent_prompt = self.factory.get_subagent_prompt()
            if subagent_prompt:
                builder.add(
                    "子代理列表", subagent_prompt,
                    is_static=False, priority=50
                )

        # 记忆系统（按 user_id 隔离）
        if self.memory:
            uid = rc.user_id if rc else ""
            memory_context = self.memory.load_memory(uid, task=task) if uid else ""
            if memory_context:
                builder.add(
                    "记忆上下文", memory_context,
                    is_static=False, priority=60
                )

        static, dynamic = builder.build()
        full = static + dynamic
        # 始终写入实例属性（子 agent initialize 时 rc 是父级的，不能依赖它）
        self._prompt_builder = builder
        self.system_static = static
        self.system_dynamic = dynamic
        self.system_prompt = full
        if rc is not None:
            rc.prompt_builder = builder
            rc.system_static = static
            rc.system_dynamic = dynamic
            rc.system_prompt = full

    def _active_prompt_builder(self):
        """当前 run 的 prompt builder；run 之外回退到实例级（initialize）"""
        rc = _current_run.get()
        if rc is not None:
            return rc.prompt_builder
        return self._prompt_builder

    def _active_system_prompt(self) -> str:
        """当前 run 的系统提示；run 之外回退到实例级"""
        rc = _current_run.get()
        if rc is not None:
            return rc.system_prompt
        return self.system_prompt

    def _update_dynamic_prompt(self, task: str = ""):
        """每轮更新动态 prompt 区块（操作当前 run 上下文中的 builder）"""
        builder = self._active_prompt_builder()
        if not builder:
            return

        if self.skill_manager:
            active_skills = self.skill_manager.get_active_skills_prompt()
            if active_skills:
                # 技能内容已通过 execute_skill 工具返回给 LLM，不重复注入 system prompt
                # 只记录已激活的技能名，避免 system prompt 膨胀
                skill_names = list(self.skill_manager._active_skills.keys())
                builder.add(
                    "已激活技能", f"已激活技能: {', '.join(skill_names)}（内容已在工具返回中，按其指导执行）",
                    is_static=False, priority=35
                )

        builder.add(
            "环境上下文", self._get_env_context(),
            is_static=False, priority=40
        )

        static, dynamic = builder.build()
        full = static + dynamic
        rc = _current_run.get()
        if rc is not None:
            rc.system_static = static
            rc.system_dynamic = dynamic
            rc.system_prompt = full
        else:
            self.system_static = static
            self.system_dynamic = dynamic
            self.system_prompt = full

    @staticmethod
    def _apply_system_messages(messages: list, static: str, dynamic: str) -> list:
        """规范化消息列表前缀为 (static, dynamic) 两条 system message。

        static 在 run 内字节稳定（可被 prompt cache 命中），dynamic 每轮更新。
        压缩层（sliding_window / context_collapse / compress_if_needed）均按 role
        过滤收集 system 消息，故双 system 安全；唯一曾假设单 system 的就是此调用点。
        """
        # 无拆分内容时保持原样
        if not static and not dynamic:
            return messages
        result = list(messages)
        while result and result[0].get("role") == "system":
            result.pop(0)
        if dynamic:
            result.insert(0, {"role": "system", "content": dynamic})
        if static:
            result.insert(0, {"role": "system", "content": static})
        return result

    def _init_task_dir(self, task: str) -> str:
        """初始化临时目录和产出目录：
        - workspace/.agent/tmp/ — 过程/临时文件，每次 run 清空
        - workspace/.agent/report/ — 报告/文档等有效产出，不清空
        """
        import shutil
        tdir = os.path.join(self.workspace, ".agent", "tmp")
        rdir = os.path.join(self.workspace, ".agent", "report")
        try:
            if os.path.isdir(tdir):
                shutil.rmtree(tdir)
            os.makedirs(tdir, exist_ok=True)
            os.makedirs(rdir, exist_ok=True)
        except Exception as e:
            logger.warning(f"重置临时目录失败: {e}")
        return tdir

    def _get_env_context(self) -> str:
        """动态生成环境上下文（git 等部分缓存30秒；task_dir 随 run 变化不缓存）"""
        now = time.time()
        if not (self._env_context_cache and (now - self._env_context_time) < 30):
            cwd = self.workspace
            is_git = os.path.exists(os.path.join(cwd, ".git"))
            branch = ""
            if is_git:
                try:
                    branch = subprocess.check_output(
                        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        stderr=subprocess.DEVNULL, timeout=5,
                        cwd=cwd
                    ).decode().strip()
                except Exception as e:
                    logger.debug(f"获取 git 分支失败: {e}")

            self._env_context_cache = (
                f"工作目录: {cwd}\n"
                f"Git 仓库: {'是' if is_git else '否'}\n"
                f"当前分支: {branch or 'N/A'}\n"
                f"平台: {platform.system()} {platform.release()}\n"
                f"模型: {self.client.model}"
            )
            self._env_context_time = now
        # task_dir 随 run 变化（任务级），不进缓存，每次动态拼接
        base = self._env_context_cache
        task_dir = current_run().task_dir
        if task_dir:
            report_dir = os.path.join(self.workspace, ".agent", "report")
            return (base
                    + f"\n临时文件目录: {task_dir}（过程/临时文件写这里）"
                    + f"\n有效产出目录: {report_dir}（报告、文档等最终成果写这里）")
        return base

    def _get_tool_summary(self) -> str:
        """生成工具描述汇总"""
        if not self.tool_registry:
            return ""
        lines = ["以下是你可以使用的工具："]
        for tool in self.tool_registry._tools.values():
            lines.append(f"- **{tool.name}**: {tool.description}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  压缩后文件状态恢复
    # ------------------------------------------------------------------ #

    def track_file_read(self, path: str, content: str):
        """记录最近读取的文件（用于压缩后恢复）"""
        if len(content) > 5000:
            content = content[:5000] + "\n... [截断]"

        self._recent_files[path] = content
        self._recent_files.move_to_end(path)
        while len(self._recent_files) > self._max_recent_files:
            self._recent_files.popitem(last=False)

    def _get_recent_files_context(self) -> str:
        """生成最近文件的上下文（用于压缩后注入）"""
        if not self._recent_files:
            return ""
        parts = ["以下是本次会话中最近读取的文件（压缩后恢复）："]
        for path, preview in self._recent_files.items():
            parts.append(f"\n### {path}\n```\n{preview}\n```")
        return "\n".join(parts)

    @property
    def tool_defs(self) -> list[dict[str, Any]]:
        tools = []

        if self.tool_registry:
            for t in self.tool_registry.get_tool_definitions():
                if t.get("function", {}).get("name") not in self.tool_denylist:
                    tools.append(t)

        if self.mcp:
            tools.extend(self.mcp.tool_defs)

        if self.skill_manager:
            tools.extend(self.skill_manager.get_tool_definitions())

        if self.plugin_manager:
            for plugin in self.plugin_manager.plugins.values():
                if plugin.enabled:
                    for t in plugin.get_tool_defs():
                        if t.get("function", {}).get("name") not in self.tool_denylist:
                            tools.append(t)

        return tools

    async def run(self, task: str, session_id: str = None, user_id: str = "", user_name: str = "", run_id: str = "") -> AgentResult:
        from hooks import get_run_id, reset_run_id, set_run_id
        # 顶层 agent 重置 ask_user 模式为交互模式
        if not self.parent_agent:
            from tools.ask_user import set_ask_user_mode, reset_ask_user_mode
            _ask_token = set_ask_user_mode("interactive")
        # 读取调用方（父级 run）上下文：子代理在同 Task 内 await 执行，可继承父级身份；
        # 顶层调用时返回空 RunContext。
        inherited = current_run()
        # 创建本次 run 的独立上下文，绑定到当前 asyncio Task —— 并发隔离的关键
        ctx = RunContext(task=task, run_id=run_id or uuid.uuid4().hex)
        # 任务级过程目录：顶层 run 建立（时间戳+任务摘要），子代理继承父目录（同任务共享）
        if self.parent_agent and inherited.task_dir:
            ctx.task_dir = inherited.task_dir
        elif not self.parent_agent:
            ctx.task_dir = self._init_task_dir(task)
        run_token = _current_run.set(ctx)

        # 建立流式事件作用域：仅顶层 run 建立；嵌套子代理 run 继承父级作用域，
        # 使整条调用树共享同一 run_id（配合 HookManager 的 run_id 过滤，杜绝并发串流）。
        hook_token = set_run_id(ctx.run_id) if not get_run_id() else None

        # 会话管理：复用 session_id 保持历史消息
        if session_id and self.session_manager:
            sess = await self.session_manager.create_session(
                session_id=session_id, system_prompt=self.system_prompt or "",
                agent_id=self.agent_id,
            )
            if self.parent_agent:
                if sess.messages:
                    old_count = len(sess.messages)
                    sess.messages = [m for m in sess.messages if m.get("role") == "system"]
                    if len(sess.messages) != old_count:
                        logger.debug(f"清除了 {old_count - len(sess.messages)} 条旧消息 (session={session_id[:16]}...)")
            if not sess.messages and self.system_prompt:
                sess.reset()
            sess.add_message("user", task)
            ctx.session = sess

        try:
            if self._is_team and self._team_config and self._team_members:
                from agent.loop import team_run_impl; return await team_run_impl(self, task, session_id, user_id, user_name)
            if self.loop_mode == "reflective":
                from agent.loop import run_impl_reflective; return await run_impl_reflective(self, task, session_id, user_id, user_name, inherited)
            from agent.loop import run_impl; return await run_impl(self, task, session_id, user_id, user_name, inherited)
        finally:
            if not self.parent_agent:
                reset_ask_user_mode(_ask_token)
            if hook_token is not None:
                reset_run_id(hook_token)
            _current_run.reset(run_token)

    async def cleanup(self):
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

        if self.session_manager:
            self.session_manager.stop_cleanup_task()

        if not self.parent_agent and self.factory:
            self.factory.stop_cleanup_task()
            await self.factory.cleanup_all()

        if self.learner and self.memory and not self.parent_agent:
            self.learner.stop_daily_task()
        if self.mcp:
            try:
                async with asyncio.timeout(10):
                    await self.mcp.close()
            except asyncio.TimeoutError:
                logger.warning(f"Agent [{self.name}] MCP close 超时(10s)，强制跳过")
            except Exception as e:
                logger.warning(f"Agent [{self.name}] MCP close 失败: {e}")

        self._cleanup_temp_dir()
        logger.info(f"Agent [{self.name}] cleaned up")

    def _cleanup_temp_dir(self):
        import shutil
        if self.temp_dir and os.path.isdir(self.temp_dir):
            try:
                shutil.rmtree(self.temp_dir, ignore_errors=True)
                logger.debug(f"临时目录已清理: {self.temp_dir}")
            except Exception as e:
                logger.warning(f"临时目录清理失败: {self.temp_dir}: {e}")
