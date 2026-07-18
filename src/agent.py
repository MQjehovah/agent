import asyncio
import contextvars
import json
import logging
import os
import platform
import re
import subprocess
import time
import uuid
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from openai.types.chat import ChatCompletionMessageParam

from learning import Learner
from prompt import PromptBuilder
from subagent_manager import SubagentManager
from utils.frontmatter import extract_frontmatter

if TYPE_CHECKING:
    from plugins import PluginManager

logger = logging.getLogger("agent.agent")


@dataclass
class AgentResult:
    agent_id: str
    status: str
    result: str
    completed_at: str = field(
        default_factory=lambda: datetime.now().isoformat())


MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("MAX_TOOL_OUTPUT_CHARS", 3000))


@dataclass
class RunContext:
    """单次 agent.run() 的执行上下文。

    通过 contextvars.ContextVar 绑定到当前 asyncio Task，因此多个并发 run()
    （Web 多请求 / webhook / scheduler / 子代理）各自拥有独立上下文，彻底消除原先
    把用户身份、会话、错误计数等写入 self 实例属性导致的竞态：
      - 用户身份串号（RBAC 越权 / 记忆隔离失效）
      - _consecutive_errors / _retry_context 跨请求互相干扰
      - self.status / self.result 返回错误结果
    """

    user_id: str = ""
    user_name: str = ""
    role: str = "default"
    session: Any = None
    task: str = ""
    consecutive_errors: int = 0
    retry_context: str = ""
    status: str = "pending"
    result: str = ""
    # 本次 run 的唯一标识，配合 HookManager 做流式事件作用域过滤
    run_id: str = ""
    # 本次 run 的分层 prompt 构建器与最终系统提示（局部化，避免并发 run 互相覆盖，
    # 也避免记忆上下文按 user_id 串号）
    system_prompt: str = ""
    prompt_builder: Any = None
    # 本次 run 的 agent_id（用于用量统计归因）
    agent_id: str = ""
    # P3: prompt cache 拆分——static 可缓存前缀，dynamic 每轮变化的尾缀
    system_static: str = ""
    system_dynamic: str = ""
    # 本次任务的过程文件目录（顶层 run 建立，子代理继承）；临时文件写这里，交付物写 workspace
    task_dir: str = ""


# 模块级 ContextVar：run() 内 set()，协程任意位置 get() 取回“当前 run”的上下文。
# asyncio Task 会拷贝上下文，故每个并发 run 天然隔离。
# 注意：default 用 None 而非共享的可变 RunContext 实例（见 ruff B039）。
_current_run: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "agent_current_run", default=None
)
# run() 之外读取时返回的只读空上下文（仅用于读取字段，不应被修改）
_EMPTY_RUN = RunContext()


def current_run() -> RunContext:
    """获取当前 run() 的执行上下文（并发安全）。run() 之外调用返回空上下文（只读）。"""
    return _current_run.get() or _EMPTY_RUN


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
        from permissions import PermissionChecker, PermissionConfig, PermissionMode
        self._permission_config = PermissionConfig(
            mode=PermissionMode(permission_mode)
        )
        self.permission = PermissionChecker(self._permission_config)

        # 钩子系统
        from hooks import HookEvent, HookManager
        self.hooks = HookManager()
        self._hook_event = HookEvent

        # 调用链路追踪
        from tracing import Tracer
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
        self._learning_per_round = get_settings().get("learning.per_round", False)
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

        from agent_session import AgentSessionManager
        from storage import init_storage
        self.session_manager = AgentSessionManager()
        await self.session_manager.start_cleanup_task()

        if self.parent_agent and self.parent_agent.storage:
            self.storage = self.parent_agent.storage
        else:
            self.storage = init_storage(self.workspace, config_dir=self.config_dir)

        from rbac import RBACManager
        self.rbac = RBACManager(self.storage)

        self._init_subagents()
        self._init_memory()

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
            from sandbox import create_sandbox, load_sandbox_config
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
        from tools.task import TaskCancelTool, TaskCreateTool, TaskGetTool, TaskListTool

        self.tool_registry = ToolRegistry()
        self.tool_registry.workspace = self.workspace

        task_manager = self.task_manager
        self.tool_registry.auto_discover(
            TaskCreateTool=TaskCreateTool(task_manager),
            TaskListTool=TaskListTool(task_manager),
            TaskGetTool=TaskGetTool(task_manager),
            TaskCancelTool=TaskCancelTool(task_manager),
        )

        # ── v2.0: 注册新工具 ──
        self._init_v2_tools()

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

    def _init_v2_tools(self):
        """注册 v2.0 新增工具"""
        try:
            from tools.code_search import CodeSearchTool
            code_search = CodeSearchTool()
            # 如果 ToolRegistry 有 register_tool 方法，直接注册
            if hasattr(self.tool_registry, 'register_tool'):
                self.tool_registry.register_tool(code_search)
        except Exception as e:
            logger.warning(f"注册 code_search 工具失败: {e}")

        try:
            from tools.batch_edit import BatchEditTool
            batch_edit = BatchEditTool()
            if hasattr(self.tool_registry, 'register_tool'):
                self.tool_registry.register_tool(batch_edit)
        except Exception as e:
            logger.warning(f"注册 batch_edit 工具失败: {e}")

        logger.debug(f"Agent [{self.name}] v2.0 工具已注册")

    def _init_code_quality(self):
        """初始化代码质量相关模块"""
        try:
            # ── .agentignore ──
            from agent_ignore import AgentIgnore
            self._agent_ignore = AgentIgnore(self.workspace)
            self._agent_ignore.inject_into(self.tool_registry)

            # 生成示例文件（如果不存在）
            if not os.path.exists(os.path.join(self.workspace, ".agentignore")):
                self._agent_ignore.generate_example(self.workspace)
        except Exception as e:
            logger.warning(f"初始化 .agentignore 失败: {e}")
            self._agent_ignore = None

        try:
            # ── 熔断器 ──
            from circuit_breaker import CircuitBreaker
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
            from git_integration import GitIntegration
            self._git = GitIntegration(self.workspace)
        except Exception as e:
            logger.warning(f"初始化 Git 集成失败: {e}")
            self._git = None

        try:
            # ── 代码质量钩子 ──
            from quality_hooks import CodeQualityHooks
            self._quality_hooks = CodeQualityHooks(self.workspace)
            if hasattr(self, 'hooks') and self.hooks:
                self._quality_hooks.register_all(self.hooks)
        except Exception as e:
            logger.warning(f"初始化质量钩子失败: {e}")
            self._quality_hooks = None

        try:
            # ── 自动技能路由 ──
            from auto_skill import AutoSkillActivator
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
                await self.mcp.connect_server(config)
            else:
                logger.debug(
                    f"跳过已禁用的 MCP server: {config.get('name', 'unnamed')}")
        self.mcp.start_health_check()

        connected = [c.get("name", "unnamed")
                     for c in self.mcp_configs if c.get("enabled", True)]
        role = "子 Agent" if subagent else "Agent"
        logger.info(
            f"{role} [{self.name}] 已连接 {len(connected)} MCP servers: {connected}")

    def _init_subagents(self):
        agents_dir = os.path.join(self.config_dir, "agents")
        if os.path.exists(agents_dir):
            self.subagent_manager = SubagentManager(agents_dir, parent_workspace=self.workspace)
            self.subagent_manager.start_cleanup_task()
            logger.info(
                f"Agent [{self.name}] 已加载 {len(self.subagent_manager.list_templates())} 个子代理: {self.subagent_manager.list_templates()}")

    def _init_memory(self):
        from memory import MemoryManager
        if self.parent_agent and self.parent_agent.memory:
            # 子 agent 复用父 memory（DB 统一，按 user_id 隔离）
            self.memory = self.parent_agent.memory
        else:
            from storage import get_storage
            self.memory = MemoryManager(storage=get_storage(), agent_id=self.agent_id)

        # 初始化自学习模块
        self.learner = Learner(
            memory_manager=self.memory,
            llm_client=self.client,
            agent_id=self.agent_id,
        )

        # 初始化自动创建模块（仅主代理）
        if not self.parent_agent:
            self.learner.init_auto_creation(
                workspace=self.config_dir,
                skill_manager=self.skill_manager,
                subagent_manager=self.subagent_manager,
            )
            # 主 agent 初始化 curator（定时提炼通用知识）
            from memory.curator import MemoryCurator
            from storage import get_storage
            self.curator = MemoryCurator(storage=get_storage(), llm_client=self.client)
            self.learner.set_curator(self.curator)

        memory_tool = self.tool_registry.get_tool("memory")
        if memory_tool and hasattr(memory_tool, 'set_memory_manager'):
            memory_tool.set_memory_manager(self.memory)

        if not self.parent_agent:
            self.learner.start_daily_task()

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
        if self.subagent_manager:
            subagent_prompt = self.subagent_manager.get_subagent_prompt()
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
        if rc is not None:
            # run 内：写入本次 run 的上下文（并发隔离）
            rc.prompt_builder = builder
            rc.system_static = static
            rc.system_dynamic = dynamic
            rc.system_prompt = full
        else:
            # initialize 路径：单线程启动期，写入实例属性作为初始值
            self._prompt_builder = builder
            self.system_static = static
            self.system_dynamic = dynamic
            self.system_prompt = full

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
        result = list(messages)
        while result and result[0].get("role") == "system":
            result.pop(0)
        if dynamic:
            result.insert(0, {"role": "system", "content": dynamic})
        result.insert(0, {"role": "system", "content": static})
        return result

    def _init_task_dir(self, task: str) -> str:
        """顶层任务建立过程文件目录：workspace/.agent/{时间戳}_{任务摘要}/artifacts/"""
        from datetime import datetime
        slug = re.sub(r"[^\w一-鿿]+", "_", task)[:20].strip("_")
        tdir = os.path.join(self.workspace, ".agent", f"{datetime.now():%Y%m%d_%H%M%S}_{slug}", "artifacts")
        try:
            os.makedirs(tdir, exist_ok=True)
        except Exception as e:
            logger.warning(f"创建任务目录失败: {e}")
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
            return base + f"\n临时文件目录: {task_dir}（过程/临时文件写这里；交付物写工作目录）"
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

        try:
            if self._is_team and self._team_config and self._team_members:
                return await self._team_run_impl(task, session_id, user_id, user_name)
            return await self._run_impl(task, session_id, user_id, user_name, inherited)
        finally:
            if hook_token is not None:
                reset_run_id(hook_token)
            # 恢复父级上下文：避免子代理的 ctx 泄漏到父级 run 的剩余逻辑
            _current_run.reset(run_token)

    async def _team_run_impl(self, task: str, session_id: str, user_id: str, user_name: str) -> "AgentResult":
        """团队模式：通过 TeamOrchestrator 编排成员执行

        v2.0: 集成 Plan Mode、AgentPool、并行执行
        """
        from agent import AgentResult
        from agent_pool import AgentPool
        from plan_mode import PlanMode
        from team.orchestrator import TeamOrchestrator
        from worktree import WorktreeManager

        # ── Plan Mode（先规划，后执行） ──
        if self._enable_plan_mode and not self._plan_mode:
            self._plan_mode = PlanMode(
                client=self.client,
                workspace=self.workspace,
                auto_plan=self._plan_mode_config.get("auto_plan", True),
                require_approval=self._plan_mode_config.get("require_approval", True),
                on_confirm=self.on_confirm,
            )

        if self._plan_mode and self._plan_mode.should_plan(task):
            self.tracer.start_span("plan_mode", attributes={"task": task[:80]})
            plan = await self._plan_mode.generate_plan(task)
            approved = await self._plan_mode.present_plan(plan)
            self.tracer.end_span(status="approved" if approved else "rejected")
            if not approved:
                return AgentResult(
                    agent_id=f"team:{self._team_config.get('name', 'unknown')}",
                    status="cancelled",
                    result="计划被用户拒绝，任务已取消",
                )

        # ── Agent 连接池 ──
        if not self._agent_pool and self.subagent_manager:
            self._agent_pool = AgentPool(
                subagent_manager=self.subagent_manager,
                max_size=10,
                default_ttl=300,
            )

        # ── Worktree 管理器 ──
        wt_manager = WorktreeManager(self.workspace)

        # ── 初始化并行编排器 ──
        orchestrator = TeamOrchestrator(
            team_name=self._team_config["name"],
            team_config=self._team_config,
            members=self._team_members,
            subagent_manager=getattr(self, 'subagent_manager', None),
            llm_client=self.client,
            memory_manager=getattr(self, 'memory', None),
            pipeline_mode=self._team_config.get("pipeline_mode", "auto"),
            progress_callback=getattr(self, '_progress_callback', None),
            parent_session_id=session_id or "",
            # v2.0 新参数
            agent_pool=self._agent_pool if hasattr(self, '_agent_pool') else None,
            worktree_manager=wt_manager,
            max_parallel=self._max_parallel,
            enable_parallel=self._enable_parallel,
        )
        try:
            result = await orchestrator.run(task)
            status = "completed" if not result.startswith("ERROR:") else "failed"

            # Plan Mode 完成
            if self._plan_mode and self._plan_mode.current_plan:
                self._plan_mode.complete_plan()

        except Exception as e:
            logger.error(f"团队编排异常: {e}")
            result = f"团队执行错误: {e}"
            status = "failed"

        # 清理 worktree
        await wt_manager.cleanup_all()

        return AgentResult(
            agent_id=f"team:{self._team_config.get('name', 'unknown')}",
            status=status,
            result=result,
        )

    async def _run_impl(self, task: str, session_id: str, user_id: str, user_name: str, inherited: RunContext) -> AgentResult:
        ctx = current_run()
        ctx.agent_id = self.agent_id
        self.status = "running"
        ctx.task = task

        # 子代理若未显式传入 user_id，则继承父级身份
        if not user_id and self.parent_agent and inherited.user_id:
            user_id = inherited.user_id
        if user_id:
            ctx.user_id = str(user_id)

        resolved_user_id = None
        resolved_user_name = user_name
        resolved_role = "default"
        if self.rbac and user_id and not self.parent_agent:
            platform_, uid = self._parse_user_id()
            info = self.rbac.resolve_user(platform=platform_, platform_uid=uid, fallback_name=user_name)
            resolved_user_id = info["user_id"]
            resolved_user_name = info["user_name"] or user_name
            resolved_role = info["role"]
        elif self.parent_agent and user_id:
            # 子代理被显式传入 user_id → 继承父级权限（如 cli:admin）
            resolved_role = "admin"
            resolved_user_id = None
        elif self.parent_agent and inherited.session:
            ps = inherited.session
            resolved_user_id = ps.user_id
            resolved_user_name = ps.user_name
            resolved_role = ps.role or "default"

        # 供 _build_prompt/_run_reflection 使用（优先用 RBAC 解析后的 id）
        if resolved_user_id:
            ctx.user_id = str(resolved_user_id)
        ctx.user_name = resolved_user_name
        ctx.role = resolved_role
        # webhook 请求默认 admin 权限（不受 RBAC 表约束）
        if user_id and user_id.startswith("webhook:"):
            ctx.role = "admin"

        if self.learner and self._learning_per_round:
            self.learner.check_user_correction(task, ctx.user_id)

        self._build_prompt(task)

        if self.skill_manager:
            self.skill_manager.clear_active_skills()

        # ── v2.0: 自动技能路由 ──
        if hasattr(self, '_auto_skill') and self._auto_skill:
            self._auto_skill.reset()
            await self._auto_skill.activate_for_task(task)

        self.tracer.start_trace(f"agent.run: {task[:50]}")

        await self.hooks.fire("agent_start", metadata={"task": task})

        from agent_session import AgentSession, AgentSessionManager
        session = None

        if self.session_manager:
            if session_id:
                session = await self.session_manager.get_session(session_id)
                if session:
                    session.add_message("user", task)
                    logger.info(
                        f"Agent [{self.name}] 复用session: {session_id}, 消息数: {len(session.messages)}")
                else:
                    session = await self.session_manager.create_session(
                        agent_id=self.agent_id,
                        session_id=session_id,
                        system_prompt=ctx.system_prompt,
                    )
                    if self.storage:
                        messages = self.storage.get_messages(session_id)
                        if messages:
                            # 注入上次压缩的历史摘要（如有），保持跨重启上下文连续性
                            get_meta = getattr(self.storage, "get_session_meta", None)
                            if get_meta:
                                try:
                                    last_summary = (get_meta(session_id) or {}).get("last_summary", "")
                                except Exception:
                                    last_summary = ""
                            else:
                                last_summary = ""
                            if last_summary:
                                messages = [
                                    {"role": "user", "content": f"[对话历史摘要]\n{last_summary}"},
                                    {"role": "assistant", "content": "已了解历史上下文，请继续。"},
                                    *messages,
                                ]
                            session.messages = cast(
                                list[ChatCompletionMessageParam], messages)
                            logger.info(
                                f"[{self.name}] 从存储恢复session: {session_id}, 消息数: {len(session.messages)}")
                    session.add_message("user", task)
                    logger.debug(
                        f"Agent [{self.name}] 创建新session: {session_id}")
            else:
                session = await self.session_manager.create_session(
                    agent_id=self.agent_id,
                    system_prompt=ctx.system_prompt,
                )
                session_id = session.session_id
                session.add_message("user", task)
                logger.info(f"Agent [{self.name}] 创建随机session: {session_id}")

            if user_id and not session.user_id:
                session.user_id = str(resolved_user_id) if resolved_user_id else ""
                session.user_name = resolved_user_name
                session.role = resolved_role
            elif not session.role:
                session.role = resolved_role

        if not session:
            session = AgentSession(
                agent_id=self.agent_id,
                session_id=session_id or "temp",
                system_prompt=ctx.system_prompt,
                user_id=resolved_user_id or "",
                user_name=resolved_user_name,
                role=resolved_role,
            )
            session.add_message("user", task)

        logger.info(
            f"[{self.name}] [{session.session_id}] 用户={session.user_name}({session.role}) 任务开始: {task}...")

        ctx.session = session

        if session.role != "admin" and self.rbac:
            # 作为动态区块加入 builder（同名区块会被替换，不会随多轮/多次 run 累积）
            ctx.prompt_builder.add(
                "权限提示",
                "当前用户权限有限，部分工具和子代理可能无法使用，遇到权限不足请友好提示用户。",
                is_static=False, priority=70,
            )
            ctx.system_static, ctx.system_dynamic = ctx.prompt_builder.build()
            ctx.system_prompt = ctx.system_static + ctx.system_dynamic

        try:
            for i in range(self.max_iterations):
                logger.debug(
                    f"Agent [{self.name}] [{session.session_id}] iteration {i + 1}")

                try:
                    # 上下文压缩检查
                    session.messages = await AgentSessionManager.compress_if_needed(
                        session.messages, self.client, tool_defs=self.tool_defs,
                        session_id=session.session_id,
                    )

                    # 追踪上下文大小
                    ctx_tokens = AgentSessionManager.estimate_tokens(
                        session.messages, self.tool_defs
                    )
                    self.tracer.record_context_size(ctx_tokens)

                    # 每轮更新动态 prompt 区块
                    self._update_dynamic_prompt(task)
                    # 注入双 system message：static（可缓存）+ dynamic（每轮变）
                    session.messages = self._apply_system_messages(
                        session.messages,
                        ctx.system_static or ctx.system_prompt,
                        ctx.system_dynamic,
                    )

                    # 清理孤立的 tool_calls（防止 session 复用时历史数据损坏）
                    session.messages = AgentSessionManager.cleanup_orphaned_tool_calls(session.messages)

                    # 思考
                    self.tracer.start_span("agent.think")
                    usage_summary = self.client.usage_tracker.get_summary()
                    logger.info(
                        f"[{self.name}] [{session.session_id}] 开始思考 | "
                        f"轮次 {i + 1}/{self.max_iterations} | "
                        f"上下文 {ctx_tokens:,}token | "
                        f"累计 {usage_summary['total_calls']}次 "
                        f"{usage_summary['total_prompt_tokens']:,}+{usage_summary['total_completion_tokens']:,}token "
                        f"¥{usage_summary['total_cost_cny']}"
                    )
                    # 第2轮起通知 UI 新起一个气泡（第1轮已在 sendMsg 中创建）
                    if i > 0:
                        await self.hooks.fire(
                            self._hook_event.ROUND_START,
                            metadata={"iteration": i + 1},
                        )
                    think_messages = session.messages
                    if ctx.retry_context:
                        think_messages = list(session.messages)
                        think_messages.append({"role": "user", "content": ctx.retry_context})
                        ctx.retry_context = ""
                    response = await self._think(think_messages)
                    self.tracer.end_span()

                    msg = response.get("message", {})
                    content = msg.get("content") or ""
                    if content:
                        await self.hooks.fire(
                            self._hook_event.LLM_RESPONSE,
                            content=content,
                            reasoning=getattr(msg, "reasoning_content", None) or "",
                        )

                    session.add_message(
                        "assistant",
                        msg.get("content") or "",
                        tool_calls=msg.get("tool_calls"),
                        reasoning_content=msg.get("reasoning_content")
                    )

                    if msg.get("tool_calls"):
                        # 并行执行工具
                        await self._execute_tool_calls_parallel(
                            msg["tool_calls"], session
                        )
                        ctx.consecutive_errors = 0
                        continue

                    if msg.get("content"):
                        ctx.status = "completed"
                        ctx.result = msg.get("content")
                        ctx.retry_context = ""
                        break

                except Exception as e:
                    ctx.consecutive_errors += 1
                    logger.error(
                        f"Agent [{self.name}] 第 {i+1} 轮出错: {e}")
                    self.tracer.end_span(status="error")

                    if ctx.consecutive_errors >= 3:
                        ctx.status = "failed"
                        ctx.result = f"连续 {ctx.consecutive_errors} 次思考出错"
                        break

                    ctx.retry_context = f"上一轮思考出错: {e}，请尝试用其他方式继续完成任务。"
                    continue
            else:
                ctx.status = "max_iterations"
                ctx.result = "达到最大迭代次数"
                logger.warning(f"Agent [{self.name}] max iterations reached")

        except asyncio.CancelledError:
            logger.warning(f"Agent [{self.name}] 任务被取消")
            ctx.status = "cancelled"
        except Exception as e:
            ctx.status = "failed"
            logger.error(
                f"Agent [{self.name}] [{session.session_id}] failed: {e}")

        # 任务结束后批量反思（后台异步，不阻塞结果返回）
        if self.learner and self._learning_per_round and session and len(session.messages) > 1:
            task_copy = task
            messages_copy = list(session.messages)
            learner = self.learner
            reflect_uid = ctx.user_id
            bg_task = asyncio.create_task(self._run_reflection(learner, task_copy, messages_copy, reflect_uid))
            self._background_tasks.add(bg_task)
            bg_task.add_done_callback(self._background_tasks.discard)

        # 触发 AGENT_STOP 钩子
        await self.hooks.fire("agent_stop", metadata={
            "status": ctx.status,
            "result_length": len(ctx.result) if ctx.result else 0,
        })

        # 用量统计落库（按 user/session/agent 归因，仅记账不拦截）
        if self.client and hasattr(self.client, "usage_tracker"):
            try:
                self.client.usage_tracker.flush()
            except Exception as e:
                logger.warning(f"用量 flush 失败: {e}")

        self.tracer.end_span(status="ok" if ctx.status == "completed" else "error")

        # 输出上下文统计
        ctx_stats = self.tracer.get_context_stats()
        if ctx_stats["samples"] > 0:
            logger.info(
                f"[{self.name}] [{session.session_id if session else ''}] 上下文统计: "
                f"峰值={ctx_stats['peak']:,}token, "
                f"最终={ctx_stats['final']:,}token, "
                f"均值={ctx_stats['avg']:,}token, "
                f"采样数={ctx_stats['samples']}"
            )

        logger.debug(
            f"Agent [{self.name}] [{session.session_id}] 任务完成: {ctx.status}")

        # 仪表盘镜像（best-effort：并发下取最后完成的 run；返回值始终来自本次 ctx，保证正确）
        self.status = ctx.status
        self.result = ctx.result or ""

        return AgentResult(
            agent_id=self.agent_id,
            status=ctx.status,
            result=ctx.result or "",
        )

    async def _execute_tool_calls_parallel(self, tool_calls: list, session):
        """并行执行工具调用"""
        if len(tool_calls) <= 1:
            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name", "")
                func_args = tc.get("function", {}).get("arguments", {})
                if isinstance(func_args, str):
                    try:
                        func_args = json.loads(func_args)
                    except (json.JSONDecodeError, ValueError):
                        func_args = {}

                try:
                    result = await self._execute_tool_safe(func_name, func_args)
                    session.add_message("tool", str(result), name=func_name,
                                        tool_call_id=tc.get("id", ""))
                except Exception as e:
                    logger.error(f"工具执行异常: {e}")
                    session.add_message("tool", f"工具执行异常: {e}", name=func_name,
                                        tool_call_id=tc.get("id", ""))
            return

        # 并行执行多个工具
        async def _run_one(tc):
            func_name = tc.get("function", {}).get("name", "")
            func_args = tc.get("function", {}).get("arguments", {})
            if isinstance(func_args, str):
                try:
                    func_args = json.loads(func_args)
                except (json.JSONDecodeError, ValueError):
                    func_args = {}
            return tc, await self._execute_tool_safe(func_name, func_args)

        tasks = [asyncio.create_task(_run_one(tc)) for tc in tool_calls]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, item in enumerate(results):
                tc = tool_calls[i]
                func_name = tc.get("function", {}).get("name", "")
                tc_id = tc.get("id", "")
                if isinstance(item, asyncio.CancelledError):
                    logger.warning("工具执行被取消")
                    session.add_message("tool", "工具执行被取消", name=func_name,
                                        tool_call_id=tc_id)
                elif isinstance(item, Exception):
                    logger.error(f"工具执行异常: {item}")
                    session.add_message("tool", f"工具执行异常: {item}", name=func_name,
                                        tool_call_id=tc_id)
                else:
                    _, result = item
                    session.add_message("tool", str(result), name=func_name,
                                        tool_call_id=tc_id)
        except asyncio.CancelledError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    def _parse_user_id(self) -> tuple[str, str]:
        uid = current_run().user_id
        if ":" in uid:
            platform_, uid = uid.split(":", 1)
            return platform_, uid
        return "dingtalk", uid

    async def _execute_tool_safe(self, name: str, args: dict) -> str:
        """带权限检查、沙箱拦截、钩子、熔断器和错误恢复的工具执行"""
        # ── v2.0: 熔断器检查 ──
        cb = getattr(self, '_circuit_breaker', None)
        if cb and name != "ask_user":
            if not cb.allow_request():
                logger.warning(f"熔断器开启，拒绝工具调用: {name}")
                return cb.get_fallback()

        # 权限检查
        perm_result = self.permission.check(name, args)
        if not perm_result:
            logger.warning(f"工具调用被拦截: {name}, 原因: {perm_result.reason}")
            return json.dumps({"success": False, "error": perm_result.reason}, ensure_ascii=False)

        role = current_run().session.role if current_run().session else ""
        if self.rbac and role and not self.rbac.check_tool(role, name):
            logger.warning(f"RBAC: 角色 [{role}] 无权执行工具 [{name}]")
            return "抱歉，您当前没有使用该功能的权限，请联系管理员开通。"

        # DEFAULT 模式需要用户确认
        if perm_result.reason == "需要用户确认" and self.on_confirm:
            try:
                confirmed = await self.on_confirm(name, args)
                if not confirmed:
                    return json.dumps({"success": False, "error": "用户拒绝执行此操作"}, ensure_ascii=False)
            except Exception as e:
                logger.error(f"用户确认回调异常: {e}")
                return json.dumps({"success": False, "error": f"用户确认回调异常: {e}"}, ensure_ascii=False)

        # 沙箱中间层拦截
        sandbox_result = await self._sandbox_intercept(name, args)
        if sandbox_result is not None:
            return sandbox_result

        # PreToolUse 钩子
        await self.hooks.fire("pre_tool_use", tool_name=name, arguments=args)

        # 插件 on_pre_tool_call 拦截
        if self.plugin_manager:
            for plugin in self.plugin_manager.plugins.values():
                if plugin.enabled:
                    try:
                        intercepted = await plugin.on_pre_tool_call(name, args)
                        if intercepted is not None:
                            logger.info(f"[插件拦截] {plugin.name} 拦截了工具调用: {name}")
                            return json.dumps(intercepted, ensure_ascii=False)
                    except Exception as e:
                        logger.error(f"插件 [{plugin.name}] on_pre_tool_call 异常: {e}")

        # 追踪
        self.tracer.start_span(f"tool.{name}")

        # 截断过长的参数用于日志显示
        args_preview = json.dumps(args, ensure_ascii=False)
        if len(args_preview) > 500:
            args_preview = args_preview[:500] + "..."

        logger.info(f"[工具调用] {name} | 输入: {args_preview}")

        await self.hooks.fire(
            self._hook_event.TOOL_START,
            tool_name=name, arguments=args,
        )

        try:
            result = await self._execute_tool(name, args)

            # ── v2.0: 熔断器记录成功 ──
            cb = getattr(self, '_circuit_breaker', None)
            if cb and name != "ask_user":
                cb.on_success()

            # 跟踪文件读取（用于压缩后恢复）
            if name == "file_operation" and args.get("operation") == "read":
                path = args.get("path", "")
                if path and '"success": true' in result:
                    try:
                        parsed = json.loads(result)
                        content = parsed.get("content", "")
                        if content:
                            self.track_file_read(path, content)
                    except (json.JSONDecodeError, ValueError):
                        pass

            result_preview = result
            if len(result_preview) > 500:
                result_preview = result_preview[:500] + "..."
            logger.info(f"[工具返回] {name} | 输出: {result_preview}")

            await self.hooks.fire(
                self._hook_event.TOOL_RESULT,
                tool_name=name, result=result_preview,
            )

            # 工具结果智能压缩（按工具类型提取关键信息）
            from tool_result_compressor import compress_tool_result
            if len(result) > MAX_TOOL_OUTPUT_CHARS:
                original_len = len(result)
                result = compress_tool_result(name, result, MAX_TOOL_OUTPUT_CHARS)
                if len(result) < original_len:
                    logger.debug(f"工具 {name} 结果压缩: {original_len} → {len(result)} 字符")

            self.tracer.end_span(status="ok")
            # PostToolUse 钩子
            await self.hooks.fire("post_tool_use", tool_name=name,
                                  arguments=args, result=result)

            # 插件 on_transform_tool_result
            if self.plugin_manager:
                for plugin in self.plugin_manager.plugins.values():
                    if plugin.enabled:
                        result = await plugin.on_transform_tool_result(name, result)

            return result
        except Exception as e:
            self.tracer.end_span(status="error")

            # ── v2.0: 熔断器记录失败 ──
            cb = getattr(self, '_circuit_breaker', None)
            if cb and name != "ask_user":
                cb.on_failure(e)

            logger.error(f"[工具异常] {name} | 错误: {type(e).__name__}: {e}")
            await self.hooks.fire("post_tool_use", tool_name=name,
                                  arguments=args, error=e)
            if self.learner and self._learning_per_round:
                args_summary = json.dumps(args, ensure_ascii=False)[:100]
                self.learner.record_failure(name, args_summary, f"{type(e).__name__}: {e}"[:150], current_run().user_id)
            # 如果熔断器开启，返回降级响应
            if cb and cb.is_open:
                return cb.get_fallback(e)
            return json.dumps({
                "success": False,
                "error": f"工具执行失败: {type(e).__name__}: {e}"
            }, ensure_ascii=False)

    async def _sandbox_intercept(self, name: str, args: dict) -> str | None:
        """沙箱中间层：拦截需要沙箱化的工具调用

        返回 None 表示放行（由工具自行执行）。
        返回 str 表示沙箱已处理，直接返回结果。
        """
        if not self.sandbox or not self.sandbox.should_intercept(name, args):
            return None

        # shell 命令 → 完全由沙箱执行
        if name == "shell":
            result = await self.sandbox.execute_shell(args)
            if result is not None:
                logger.info(f"[沙箱拦截] shell → {result.get('sandbox', '?')}")
                return json.dumps(result, ensure_ascii=False)

        # file/edit 工具 → 仅路径验证，放行由工具自行执行
        if name in ("file_operation", "edit"):
            path = args.get("path", "")
            if path:
                valid, reason = self.sandbox.validate_path(path)
                if not valid:
                    return json.dumps({"success": False, "error": reason}, ensure_ascii=False)

        return None

    async def _run_reflection(self, learner, task: str, messages: list, user_id: str = ""):
        """后台执行任务反思，不阻塞主流程"""
        try:
            logger.info(f"[自学习] 开始任务反思, 消息数: {len(messages)}")
            saved = await learner.reflect_on_task(task, messages, user_id)
            if saved > 0:
                logger.info(f"[自学习] 任务反思完成，保存了 {saved} 条经验")
            else:
                logger.info("[自学习] 任务反思完成，无新经验保存")
        except Exception as e:
            logger.warning(f"[自学习] 任务反思失败: {e}", exc_info=True)

    def _has_token_subscribers(self) -> bool:
        return bool(self.hooks._hooks.get(self._hook_event.CHAT_EVENT))

    async def _think(self, messages: Sequence[ChatCompletionMessageParam]) -> dict[str, Any]:
        try:
            if self._has_token_subscribers():
                return await self._think_stream(messages)

            # 非流式模式
            response = await self.client.chat(
                messages,
                self.tool_defs,
                stream=False
            )

            if not response.choices:
                logger.error(f"Agent [{self.name}] no choices returned from LLM")
                raise Exception("No choices returned")

            choice = response.choices[0]
            msg = choice.message

            # LLM 响应 info 日志
            content_preview = (msg.content or "")[:200]
            logger.info(f"[LLM响应] model: {response.model} | content: {content_preview or '(空)'} | tool_calls: {len(msg.tool_calls) if msg.tool_calls else 0}")

            tool_calls = None
            if msg.tool_calls:
                tool_calls = []
                for tc in msg.tool_calls:
                    func_args = tc.function.arguments
                    if isinstance(func_args, str):
                        try:
                            json.loads(func_args)
                        except (json.JSONDecodeError, ValueError):
                            try:
                                func_args = json.dumps(func_args, ensure_ascii=False)
                            except (TypeError, ValueError):
                                func_args = "{}"
                    elif isinstance(func_args, dict):
                        logger.warning(f"Agent [{self.name}] function.arguments is dict, converting to JSON string")
                        func_args = json.dumps(func_args, ensure_ascii=False)
                    else:
                        logger.warning(f"Agent [{self.name}] function.arguments is {type(func_args)}, set to empty object")
                        func_args = "{}"
                    tool_calls.append({
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": func_args
                        }
                    })

            return {
                "message": {
                    "content": msg.content,
                    "tool_calls": tool_calls,
                    "reasoning_content": getattr(msg, "reasoning_content", None)
                }
            }
        except Exception as e:
            logger.error(f"Agent [{self.name}] think error: {e}")
            return {"message": {"content": f"思考出错: {e}"}}

    async def _think_stream(self, messages):
        """流式思考模式 — 实时推送 token 给调用方"""
        content = ""
        reasoning_content = ""
        tool_calls_accumulator = {}

        async for chunk in self.client.stream_chat(messages, self.tool_defs):
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            if delta and getattr(delta, "reasoning_content", None):
                reasoning_content += delta.reasoning_content

            if delta and delta.content:
                content += delta.content
                await self.hooks.fire(self._hook_event.CHAT_EVENT, token=delta.content)

            if delta and delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_accumulator:
                        tool_calls_accumulator[idx] = {
                            "id": tc.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""}
                        }
                    if tc.function:
                        if tc.function.name:
                            tool_calls_accumulator[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_accumulator[idx]["function"]["arguments"] += tc.function.arguments

        tool_calls = list(tool_calls_accumulator.values()) if tool_calls_accumulator else None
        return {
            "message": {
                "content": content or None,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning_content or None
            }
        }

    async def _execute_tool(self, name: str, args: dict) -> str:
        # 提取当前用户 ID：优先 session.user_id，回退 ctx.user_id（均来自当前 run 上下文）
        rc = current_run()
        current_uid = ""
        if rc.session and rc.session.user_id:
            current_uid = rc.session.user_id
        elif rc.user_id:
            current_uid = rc.user_id

        try:
            if name == "subagent" and self.subagent_manager:
                return await self._execute_subagent(args)

            if self.tool_registry and self.tool_registry.has_tool(name):
                if name == "memory":
                    args["_local_user_id"] = current_uid
                return await self.tool_registry.execute(name, args)

            if self.skill_manager and name in ("skill", "execute_skill"):
                return await self.skill_manager.execute_tool(name, args)

            if self.mcp and self.mcp.has_tool(name):
                return await self.mcp.call_tool(name, args)

            if self.plugin_manager:
                for plugin in self.plugin_manager.plugins.values():
                    logger.debug(f"检查插件 {plugin.name}, enabled={plugin.enabled}")
                    if plugin.enabled:
                        tool_defs = plugin.get_tool_defs()
                        logger.debug(
                            f"插件 {plugin.name} 工具定义: {[t.get('function', {}).get('name') for t in tool_defs]}")
                        if any(t.get("function", {}).get("name") == name for t in tool_defs):
                            logger.info(f"执行插件工具: {plugin.name}.{name}")
                            if current_uid:
                                args["_local_user_id"] = current_uid
                            return await plugin.execute_tool(name, args)

            return f"工具 {name} 不存在"
        except Exception as e:
            return f"工具执行错误: {e}"

    async def _execute_subagent(self, args: dict) -> str:
        task = args.get("task")
        if not task:
            return json.dumps({"success": False, "error": "缺少task参数"}, ensure_ascii=False)

        agent_name = args.get("name", "") or args.get("template", "")
        # name 是自由描述（如"截图双屏Bug修复"），template 才是团队/模板名
        template_name = args.get("template", "") or args.get("name", "")
        display_name = f"{args['template']} → {args['name']}" if args.get("template") and args.get("name") else agent_name

        role = current_run().session.role if current_run().session else ""
        if self.rbac and role and agent_name and not self.rbac.check_agent(role, agent_name):
            logger.warning(f"RBAC: 角色 [{role}] 无权访问子代理 [{agent_name}]")
            return "抱歉，您当前没有使用该功能的权限，请联系管理员开通。"

        await self.hooks.fire(
            self._hook_event.SUBAGENT_START,
            metadata={"name": display_name, "task": task},
        )

        try:
            if self.subagent_manager and self.subagent_manager.is_team(template_name):
                # 团队工具调用：创建 session 后走编排器
                if args.get("session_id") and self.session_manager:
                    sess = await self.session_manager.get_session(args["session_id"])
                    if not sess:
                        sess = await self.session_manager.create_session(
                            agent_id=f"team:{agent_name}", session_id=args["session_id"])
                        if self.storage:
                            try:
                                msgs = self.storage.get_messages(args["session_id"])
                                if msgs:
                                    sess.messages = msgs
                            except Exception:
                                pass
                    if sess:
                        sess.add_message("user", task)

                async def _team_progress(stage: str, status: str, info, extra=None):
                    await self.hooks.fire(
                        self._hook_event.SUBAGENT_PROGRESS,
                        metadata={"stage": stage, "status": status,
                                  "info": info, "extra": extra,
                                  "team": display_name},
                    )

                result = await self.subagent_manager._run_team_orchestrator(
                    task, template_name,
                    progress_callback=_team_progress,
                    parent_session_id=args.get("session_id", ""))
            else:
                # 个人子代理：直连 agent.run()
                instance, is_new = await self.subagent_manager.get_or_create_subagent(
                    template=args.get("template", ""),
                    name=args.get("name", ""),
                    session_id=args.get("session_id", ""),
                    system_prompt=args.get("system_prompt", ""),
                    tools=args.get("tools"),
                    mcp_servers=args.get("mcp_servers"),
                    client=self.client,
                    parent_agent=self,
                )
                # 注册子代理事件转发钩子
                self._register_subagent_hooks(instance.agent, display_name)
                try:
                    result = await instance.agent.run(task)
                    instance.task_count += 1
                    instance.last_used = time.time()
                except Exception as e:
                    result = AgentResult(
                        agent_id=instance.agent.agent_id,
                        status="failed",
                        result=f"子代理执行错误: {e}",
                    )
                    logger.error(f"子代理执行错误: {e}")
                finally:
                    self._unregister_subagent_hooks(instance.agent)
                if not args.get("keep_alive", True):
                    await self.subagent_manager.cleanup_subagent(instance.session_id)
        except Exception as e:
            logger.error(f"Subagent execution error: {e}")
            await self.hooks.fire(
                self._hook_event.SUBAGENT_RESULT,
                metadata={"name": display_name, "error": str(e)},
            )
            return json.dumps({"success": False, "error": f"子代理执行错误: {e}"}, ensure_ascii=False)

        stats = self.subagent_manager.get_stats() if self.subagent_manager else {}

        result_preview = result.result or ""
        if len(result_preview) > 500:
            result_preview = result_preview[:500] + "..."

        await self.hooks.fire(
            self._hook_event.SUBAGENT_RESULT,
            metadata={"name": display_name, "status": result.status, "result": result_preview},
        )

        return json.dumps({
            "success": result.status == "completed",
            "agent_id": result.agent_id,
            "status": result.status,
            "result": result.result,
            "active_subagents": stats.get("active_count", 0),
        }, ensure_ascii=False)

    def _register_subagent_hooks(self, sub_agent, agent_name: str):
        """在子代理上注册事件转发钩子，将子代理的中间事件转发到父代理的 SUBAGENT_* 事件。"""
        mapping = {
            self._hook_event.TOOL_START: self._hook_event.SUBAGENT_TOOL_START,
            self._hook_event.TOOL_RESULT: self._hook_event.SUBAGENT_TOOL_RESULT,
            self._hook_event.ROUND_START: self._hook_event.SUBAGENT_ROUND_START,
            self._hook_event.CHAT_EVENT: self._hook_event.SUBAGENT_CHAT_EVENT,
            self._hook_event.LLM_RESPONSE: self._hook_event.SUBAGENT_LLM_RESPONSE,
        }
        self._subagent_hook_unregisters = []
        for src_evt, dst_evt in mapping.items():

            async def _forward(ctx, _dst=dst_evt, _name=agent_name):
                ctx.event = _dst
                ctx.agent_name = _name
                await self.hooks.fire(_dst, **{
                    "token": ctx.token,
                    "content": ctx.content,
                    "tool_name": ctx.tool_name,
                    "arguments": ctx.arguments,
                    "result": ctx.result,
                    "agent_name": _name,
                    "agent_type": "subagent",
                    "metadata": ctx.metadata,
                })
            sub_agent.hooks.register(src_evt, _forward)
            self._subagent_hook_unregisters.append((sub_agent, src_evt, _forward))

    def _unregister_subagent_hooks(self, sub_agent):
        for sa, evt, cb in getattr(self, "_subagent_hook_unregisters", []):
            if sa is sub_agent:
                sa.hooks.unregister(evt, cb)
        self._subagent_hook_unregisters = []

    async def cleanup(self):
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

        if self.session_manager:
            self.session_manager.stop_cleanup_task()

        if not self.parent_agent and self.subagent_manager:
            self.subagent_manager.stop_cleanup_task()
            await self.subagent_manager.cleanup_all()

        if self.memory and not self.parent_agent:
            self.learner.stop_daily_task()
        if self.mcp:
            try:
                # 超时保护：避免 cleanup 因 MCP stdio 子进程关闭而长时间阻塞
                await asyncio.wait_for(self.mcp.close(), timeout=10)
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
