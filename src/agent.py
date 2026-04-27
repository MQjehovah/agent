import logging
import asyncio
import platform
import re
import subprocess
from typing import Optional, Dict, Any, List, TYPE_CHECKING, cast, Sequence
from dataclasses import dataclass, field
from collections import OrderedDict
from datetime import datetime
import os
import json
from openai.types.chat import ChatCompletionMessageParam

from utils.frontmatter import extract_frontmatter
from subagent_manager import SubagentManager
from prompt import PromptBuilder
from learning import Learner

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


MAX_TOOL_OUTPUT_CHARS = 3000

class Agent:
    def __init__(
        self,
        workspace: str,
        client,
        parent_agent: "Agent" = None,
        permission_mode: str = "auto",
    ):
        self.workspace = workspace
        self.client = client
        self.parent_agent = parent_agent

        self.agent_id = ""
        self.name = ""
        self.description = ""
        self.system_prompt = ""
        self.system_prompt_raw = ""  # PROMPT.md 原始内容（不含技能/子代理追加）
        self.max_iterations = 100

        # Prompt 分层拼装器
        self._prompt_builder: Optional[PromptBuilder] = None

        # 压缩后状态恢复：最近读取的文件
        self._recent_files: OrderedDict[str, str] = OrderedDict()
        self._max_recent_files = 5

        self.tool_registry = None
        self.mcp = None
        self.skill_manager = None
        self.subagent_manager = None
        self.session_manager = None
        self.storage = None
        self.plugin_manager: Optional["PluginManager"] = None
        self.memory = None
        self.learner: Optional[Learner] = None
        self._background_tasks: set = set()

        self.status = "pending"
        self.result: Optional[str] = None

        # 权限系统
        from permissions import PermissionChecker, PermissionConfig, PermissionMode
        self.permission = PermissionChecker(PermissionConfig(
            mode=PermissionMode(permission_mode)
        ))

        # 钩子系统
        from hooks import HookManager
        self.hooks = HookManager()

        # 调用链路追踪
        from tracing import Tracer
        self.tracer = Tracer()

        # 后台任务管理器
        from tools.task import TaskManager
        self.task_manager = TaskManager()

        # 用户确认回调（外部注入，如交互模式中的 input()）
        self.on_confirm = None

        # 流式输出回调
        self.on_token = None

        self._env_context_cache: str = ""
        self._env_context_time: float = 0.0

        self._consecutive_errors = 0
        self._current_task: str = ""
        self._retry_context: str = ""

    async def initialize(self, session_id: str = None):
        self._load_system_prompt()
        self._init_tools()
        self._init_skills()
        await self._load_mcp_servers()

        from agent_session import AgentSessionManager
        from storage import init_storage
        self.session_manager = AgentSessionManager()
        await self.session_manager.start_cleanup_task()

        if self.parent_agent and self.parent_agent.storage:
            self.storage = self.parent_agent.storage
        else:
            self.storage = init_storage(self.workspace)

        self._init_subagents()
        self._init_memory()

        # 构建分层 prompt（必须在所有初始化完成后）
        self._build_prompt()

    def _load_system_prompt(self):
        prompt_file = os.path.join(self.workspace, "PROMPT.md")

        if not os.path.exists(prompt_file):
            logger.warning(f"No PROMPT.md found in {self.workspace}")
            self.agent_id = self.name = ""
            return

        with open(prompt_file, "r", encoding="utf-8") as f:
            content = f.read()

        frontmatter, body = extract_frontmatter(content)

        if frontmatter:
            self.agent_id = self.name = frontmatter.get("name", "")
            self.description = frontmatter.get("description", "")
            if isinstance(self.description, str):
                self.description = self.description.strip()

        self.system_prompt = self._expand_env_vars(body.strip()) if body else ""
        self.system_prompt_raw = self.system_prompt

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
        from tools import TodoTool, FileTool, SubagentTool, MemoryTool, ShellTool
        from tools.grep import GrepTool
        from tools.glob import GlobTool
        from tools.edit import EditTool
        from tools.code_preview import CodePreviewTool
        from tools.web import WebSearchTool, WebFetchTool
        from tools.task import TaskCreateTool, TaskListTool, TaskGetTool, TaskCancelTool
        from tools.ask_user import AskUserTool

        self.tool_registry = ToolRegistry()

        # 核心工具
        self.tool_registry.register_tool(TodoTool())
        self.tool_registry.register_tool(FileTool())
        self.tool_registry.register_tool(SubagentTool())
        self.tool_registry.register_tool(MemoryTool())
        self.tool_registry.register_tool(ShellTool())

        # 新增：搜索与编辑工具
        self.tool_registry.register_tool(GrepTool())
        self.tool_registry.register_tool(GlobTool())
        self.tool_registry.register_tool(EditTool())
        self.tool_registry.register_tool(CodePreviewTool())

        # 新增：Web 工具
        self.tool_registry.register_tool(WebSearchTool())
        self.tool_registry.register_tool(WebFetchTool())

        # 新增：后台任务工具
        self.tool_registry.register_tool(TaskCreateTool(self.task_manager))
        self.tool_registry.register_tool(TaskListTool(self.task_manager))
        self.tool_registry.register_tool(TaskGetTool(self.task_manager))
        self.tool_registry.register_tool(TaskCancelTool(self.task_manager))

        # 用户交互工具暂不注册（当前流程不支持交互式确认）
        # self.tool_registry.register_tool(AskUserTool())

        logger.info(
            f"Agent [{self.name}] 已注册 {len(self.tool_registry.list_tools())} 个工具: {self.tool_registry.list_tools()}")

    def _init_skills(self):
        skills_dir = os.path.join(self.workspace, "skills")
        if os.path.exists(skills_dir):
            from skills import SkillManager
            self.skill_manager = SkillManager(skills_dir)
            logger.info(
                f"Agent [{self.name}] 已加载 {len(self.skill_manager.list_skills())} 个技能: {[self.skill_manager.list_skills()]}")

    async def _load_mcp_servers(self):
        mcp_file = os.path.join(self.workspace, "mcp_servers.json")
        self.mcp_configs = []

        if os.path.exists(mcp_file):
            try:
                with open(mcp_file, "r", encoding="utf-8") as f:
                    self.mcp_configs = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load mcp_servers.json: {e}")

        if self.mcp_configs:
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
            logger.info(
                f"Agent [{self.name}] 已连接 {len(connected)} MCP servers: {connected}")

    def _init_subagents(self):
        agents_dir = os.path.join(self.workspace, "agents")
        if os.path.exists(agents_dir):
            self.subagent_manager = SubagentManager(agents_dir)
            self.subagent_manager.start_cleanup_task()
            logger.info(
                f"Agent [{self.name}] 已加载 {len(self.subagent_manager.list_templates())} 个子代理: {self.subagent_manager.list_templates()}")

    def _init_memory(self):
        from memory import MemoryManager
        self.memory = MemoryManager(self.workspace, agent_id=self.agent_id)
        self.memory.set_llm_client(self.client)

        if self.parent_agent and self.parent_agent.memory:
            self.memory.shared_knowledge_file = self.parent_agent.memory.shared_knowledge_file

        # 初始化自学习模块
        self.learner = Learner(
            memory_manager=self.memory,
            llm_client=self.client,
            agent_id=self.agent_id,
        )

        # 初始化自动创建模块（仅主代理）
        if not self.parent_agent:
            self.learner.init_auto_creation(
                workspace=self.workspace,
                skill_manager=self.skill_manager,
                subagent_manager=self.subagent_manager,
            )

        memory_tool = self.tool_registry.get_tool("memory")
        if memory_tool and hasattr(memory_tool, 'set_memory_manager'):
            memory_tool.set_memory_manager(self.memory)

        if not self.parent_agent:
            self.learner.start_daily_task()

    # ------------------------------------------------------------------ #
    #  Prompt 分层拼装
    # ------------------------------------------------------------------ #

    def _build_prompt(self, task: str = ""):
        """使用 PromptBuilder 构建分层 prompt"""
        self._prompt_builder = PromptBuilder()

        # === 静态区 (可被 LLM prompt cache 缓存) ===
        self._prompt_builder.add(
            "角色定义", self.system_prompt_raw,
            is_static=True, priority=0
        )
        self._prompt_builder.add(
            "工具列表", self._get_tool_summary(),
            is_static=True, priority=10
        )
        self._prompt_builder.add(
            "工具使用指南", self._get_tool_guidelines(),
            is_static=True, priority=20
        )

        # === 动态区 (每轮可能变化) ===
        self._prompt_builder.add(
            "环境上下文", self._get_env_context(),
            is_static=False, priority=30
        )

        # 技能列表
        if self.skill_manager:
            skills_prompt = self.skill_manager.get_skills_prompt()
            if skills_prompt:
                self._prompt_builder.add(
                    "技能列表", skills_prompt,
                    is_static=False, priority=40
                )

        # 子代理列表
        if self.subagent_manager:
            subagent_prompt = self.subagent_manager.get_subagent_prompt()
            if subagent_prompt:
                self._prompt_builder.add(
                    "子代理列表", subagent_prompt,
                    is_static=False, priority=50
                )

                # 记忆上下文（按任务相关性筛选）
        
        # 记忆系统
        if self.memory:
            memory_context = self._load_memory_context_sync(task)
            if memory_context:
                self._prompt_builder.add(
                    "记忆上下文", memory_context,
                    is_static=False, priority=60
                )

        self.system_prompt = self._prompt_builder.build_full()

    def _update_dynamic_prompt(self, task: str = ""):
        """每轮更新动态 prompt 区块"""
        if not self._prompt_builder:
            return

        # 更新环境上下文
        self._prompt_builder.add(
            "环境上下文", self._get_env_context(),
            is_static=False, priority=40
        )

        # 记忆上下文仅在首轮构建，迭代中不再重复搜索
        # 注入最近文件恢复上下文
        # recent_context = self._get_recent_files_context()
        # if recent_context:
        #     self._prompt_builder.add(
        #         "最近读取文件", recent_context,
        #         is_static=False, priority=100
        #     )

        self.system_prompt = self._prompt_builder.build_full()

    def _get_env_context(self) -> str:
        """动态生成环境上下文（结果缓存30秒，避免每轮都执行git命令）"""
        import time
        now = time.time()
        if self._env_context_cache and (now - self._env_context_time) < 30:
            return self._env_context_cache

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
            except Exception:
                pass

        self._env_context_cache = (
            f"工作目录: {cwd}\n"
            f"Git 仓库: {'是' if is_git else '否'}\n"
            f"当前分支: {branch or 'N/A'}\n"
            f"平台: {platform.system()} {platform.release()}\n"
            f"模型: {self.client.model}"
        )
        self._env_context_time = now
        return self._env_context_cache

    async def _get_memory_context(self, task: str) -> str:
        """获取与当前任务相关的记忆（优先使用相关性搜索）"""
        if not task:
            return self.memory.load_memory("")

        try:
            from memory.relevance import find_relevant_memories
            relevant = await find_relevant_memories(
                task, self.memory, self.client
            )
            if relevant:
                return "以下是与当前任务相关的记忆:\n" + "\n\n".join(relevant)
        except Exception:
            pass

        return self.memory.load_memory(task)

    def _load_memory_context_sync(self, task: str) -> str:
        """同步加载记忆上下文（纯关键词匹配，不调用LLM）"""
        if not self.memory:
            return ""

        if not task:
            return self.memory.load_memory("")

        try:
            from memory.relevance import _keyword_search, _search_shared_knowledge
            keyword_results = _keyword_search(task, self.memory, max_results=5)
            shared_results = _search_shared_knowledge(task, self.memory, max_results=2)
            keyword_results.extend(r for r in shared_results if r not in keyword_results)

            if keyword_results:
                return "以下是与当前任务相关的记忆:\n" + "\n\n".join(keyword_results)
        except Exception:
            pass

        return self.memory.load_memory(task)

    def _get_tool_guidelines(self) -> str:
        return """### 工具使用规则

1. **优先使用专用工具而非 shell 命令：**
   - 读文件 → file_operation(read) 而非 cat/head/tail
   - 编辑文件 → edit 而非 sed/awk
   - 写文件 → file_operation(write) 而非 echo/heredoc
   - 搜索文件名 → glob 而非 find/ls
   - 搜索内容 → grep 而非 grep/rg 命令
   - shell 仅用于没有专用工具的系统命令

2. **代码分析遵循 "先搜后读" 策略：**
   - 第一步：glob 找文件列表 → grep 搜索关键函数/类
   - 第二步：file_operation(read, offset=行号, limit=50) 精确读取
   - 禁止一次性读取整个项目

3. **file_operation(read) 使用规则：**
   - 默认只读 200 行，可通过 offset 和 limit 分段读取大文件
   - 大文件先用 grep 找行号，再用 offset+limit 精确读取
   - 多个文件并行读取时，每个 limit 控制在 50-100 行

4. **edit 使用规则：**
   - old_text 必须精确匹配，提供足够上下文使其唯一
   - 修改前先 file_operation(read) 确认内容

5. **多工具调用：**
   - 独立的操作可以并行调用
   - 有依赖关系的操作必须顺序执行"""

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
    def tool_defs(self) -> List[Dict[str, Any]]:
        tools = []

        if self.tool_registry:
            tools.extend(self.tool_registry.get_tool_definitions())

        if self.mcp:
            tools.extend(self.mcp.tool_defs)

        if self.skill_manager:
            tools.extend(self.skill_manager.get_tool_definitions())

        if self.plugin_manager:
            for plugin in self.plugin_manager.plugins.values():
                if plugin.enabled:
                    tools.extend(plugin.get_tool_defs())

        return tools

    async def run(self, task: str, session_id: str = None) -> AgentResult:
        self.status = "running"
        self._consecutive_errors = 0
        self._current_task = task

        if self.learner:
            self.learner.check_user_correction(task)

        # 根据当前任务重新拼装 prompt
        self._build_prompt(task)

        # 开始追踪
        self.tracer.start_trace(f"agent.run: {task[:50]}")

        # 触发 AGENT_START 钩子
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
                        system_prompt=self.system_prompt,
                    )
                    if self.storage:
                        messages = self.storage.get_messages(session_id)
                        if messages:
                            session.messages = cast(
                                List[ChatCompletionMessageParam], messages)
                            logger.info(
                                f"Agent [{self.name}] 从存储恢复session: {session_id}, 消息数: {len(session.messages)}")
                    session.add_message("user", task)
                    logger.debug(
                        f"Agent [{self.name}] 创建新session: {session_id}")
            else:
                session = await self.session_manager.create_session(
                    agent_id=self.agent_id,
                    system_prompt=self.system_prompt,
                )
                session_id = session.session_id
                session.add_message("user", task)
                logger.info(f"Agent [{self.name}] 创建随机session: {session_id}")

        if not session:
            session = AgentSession(
                agent_id=self.agent_id,
                session_id=session_id or "temp",
                system_prompt=self.system_prompt
            )
            session.add_message("user", task)

        logger.info(
            f"Agent [{self.name}] [{session.session_id}] 任务开始: {task}...")

        try:
            for i in range(self.max_iterations):
                logger.debug(
                    f"Agent [{self.name}] [{session.session_id}] iteration {i + 1}")

                try:
                    # 上下文压缩检查
                    session.messages = await AgentSessionManager.compress_if_needed(
                        session.messages, self.client, tool_defs=self.tool_defs
                    )

                    # 追踪上下文大小
                    ctx_tokens = AgentSessionManager.estimate_tokens(
                        session.messages, self.tool_defs
                    )
                    self.tracer.record_context_size(ctx_tokens)

                    # 每轮更新动态 prompt 区块
                    self._update_dynamic_prompt(task)
                    if session.messages and session.messages[0].get("role") == "system":
                        session.messages[0]["content"] = self.system_prompt

                    # 思考
                    self.tracer.start_span("agent.think")
                    usage_summary = self.client.usage_tracker.get_summary()
                    logger.info(
                        f"[{self.name}] [{session.session_id}] 开始思考 | "
                        f"轮次 {i + 1}/{self.max_iterations} | "
                        f"上下文 {ctx_tokens:,}tok | "
                        f"累计 {usage_summary['total_calls']}次 "
                        f"{usage_summary['total_prompt_tokens']:,}+{usage_summary['total_completion_tokens']:,}tok "
                        f"¥{usage_summary['total_cost_cny']}"
                    )
                    think_messages = session.messages
                    if self._retry_context:
                        think_messages = list(session.messages)
                        think_messages.append({"role": "user", "content": self._retry_context})
                        self._retry_context = ""
                    response = await self._think(think_messages)
                    self.tracer.end_span()

                    msg = response.get("message", {})

                    session.add_message(
                        "assistant",
                        msg.get("content") or "",
                        tool_calls=msg.get("tool_calls")
                    )

                    if msg.get("tool_calls"):
                        # 并行执行工具
                        await self._execute_tool_calls_parallel(
                            msg["tool_calls"], session
                        )
                        self._consecutive_errors = 0
                        continue

                    if msg.get("content"):
                        self.status = "completed"
                        self.result = msg.get("content")
                        self._retry_context = ""
                        break

                except Exception as e:
                    self._consecutive_errors += 1
                    logger.error(
                        f"Agent [{self.name}] 第 {i+1} 轮出错: {e}")
                    self.tracer.end_span(status="error")

                    if self._consecutive_errors >= 3:
                        self.status = "failed"
                        self.result = f"连续 {self._consecutive_errors} 次思考出错"
                        break

                    self._retry_context = f"上一轮思考出错: {e}，请尝试用其他方式继续完成任务。"
                    continue
            else:
                self.status = "max_iterations"
                self.result = "达到最大迭代次数"
                logger.warning(f"Agent [{self.name}] max iterations reached")

        except asyncio.CancelledError:
            logger.warning(f"Agent [{self.name}] 任务被取消")
            self.status = "cancelled"
        except Exception as e:
            self.status = "failed"
            logger.error(
                f"Agent [{self.name}] [{session.session_id}] failed: {e}")

        # 任务结束后批量反思（后台异步，不阻塞结果返回）
        if self.learner and session and len(session.messages) > 1:
            task_copy = task
            messages_copy = list(session.messages)
            learner = self.learner
            bg_task = asyncio.create_task(self._run_reflection(learner, task_copy, messages_copy))
            self._background_tasks.add(bg_task)
            bg_task.add_done_callback(self._background_tasks.discard)

        # 触发 AGENT_STOP 钩子
        await self.hooks.fire("agent_stop", metadata={
            "status": self.status,
            "result_length": len(self.result) if self.result else 0,
        })

        self.tracer.end_span(status="ok" if self.status == "completed" else "error")

        # 输出上下文统计
        ctx_stats = self.tracer.get_context_stats()
        if ctx_stats["samples"] > 0:
            logger.info(
                f"[{session.session_id if session else ''}] 上下文统计: "
                f"峰值={ctx_stats['peak']:,}tok, "
                f"最终={ctx_stats['final']:,}tok, "
                f"均值={ctx_stats['avg']:,}tok, "
                f"采样数={ctx_stats['samples']}"
            )

        logger.debug(
            f"Agent [{self.name}] [{session.session_id}] 任务完成: {self.status}")

        return AgentResult(
            agent_id=self.agent_id,
            status=self.status,
            result=self.result or "",
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

                result = await self._execute_tool_safe(func_name, func_args)
                session.add_message("tool", str(result), name=func_name,
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
                if isinstance(item, asyncio.CancelledError):
                    logger.warning(f"工具执行被取消")
                    session.add_message("tool", "工具执行被取消", name=tool_calls[i].get("function", {}).get("name", ""))
                elif isinstance(item, Exception):
                    logger.error(f"工具执行异常: {item}")
                    session.add_message("tool", f"工具执行异常: {item}")
                else:
                    tc, result = item
                    func_name = tc.get("function", {}).get("name", "")
                    session.add_message("tool", str(result), name=func_name,
                                        tool_call_id=tc.get("id", ""))
        except asyncio.CancelledError:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _execute_tool_safe(self, name: str, args: Dict) -> str:
        """带权限检查、钩子和错误恢复的工具执行"""
        # 权限检查
        perm_result = self.permission.check(name, args)
        if not perm_result:
            logger.warning(f"工具调用被拦截: {name}, 原因: {perm_result.reason}")
            return json.dumps({"success": False, "error": perm_result.reason}, ensure_ascii=False)

        # DEFAULT 模式需要用户确认
        if perm_result.reason == "需要用户确认" and self.on_confirm:
            confirmed = await self.on_confirm(name, args)
            if not confirmed:
                return json.dumps({"success": False, "error": "用户拒绝执行此操作"}, ensure_ascii=False)

        # PreToolUse 钩子
        await self.hooks.fire("pre_tool_use", tool_name=name, arguments=args)

        # 追踪
        self.tracer.start_span(f"tool.{name}")

        # 截断过长的参数用于日志显示
        args_preview = json.dumps(args, ensure_ascii=False)
        if len(args_preview) > 500:
            args_preview = args_preview[:500] + "..."

        logger.info(f"[工具调用] {name} | 输入: {args_preview}")

        try:
            result = await self._execute_tool(name, args)

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

            # 全局工具输出截断保护
            if len(result) > MAX_TOOL_OUTPUT_CHARS:
                result = result[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [工具输出已截断，原始长度 {len(result)} 字符]"

            self.tracer.end_span(status="ok")
            # PostToolUse 钩子
            await self.hooks.fire("post_tool_use", tool_name=name,
                                  arguments=args, result=result)

            return result
        except Exception as e:
            self.tracer.end_span(status="error")
            logger.error(f"[工具异常] {name} | 错误: {type(e).__name__}: {e}")
            await self.hooks.fire("post_tool_use", tool_name=name,
                                  arguments=args, error=e)
            if self.learner:
                args_summary = json.dumps(args, ensure_ascii=False)[:100]
                self.learner.record_failure(name, args_summary, f"{type(e).__name__}: {e}"[:150])
            return json.dumps({
                "success": False,
                "error": f"工具执行失败: {type(e).__name__}: {e}"
            }, ensure_ascii=False)

    async def _run_reflection(self, learner, task: str, messages: list):
        """后台执行任务反思，不阻塞主流程"""
        try:
            logger.info(f"[自学习] 开始任务反思, 消息数: {len(messages)}")
            saved = await learner.reflect_on_task(task, messages)
            if saved > 0:
                logger.info(f"[自学习] 任务反思完成，保存了 {saved} 条经验")
            else:
                logger.info("[自学习] 任务反思完成，无新经验保存")
        except Exception as e:
            logger.warning(f"[自学习] 任务反思失败: {e}", exc_info=True)

    async def _think(self, messages: Sequence[ChatCompletionMessageParam]) -> Dict[str, Any]:
        try:
            # 流式模式
            if self.on_token:
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
            logger.info(f"[LLM响应] content: {content_preview or '(空)'} | tool_calls: {len(msg.tool_calls) if msg.tool_calls else 0}")

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
                    "tool_calls": tool_calls
                }
            }
        except Exception as e:
            logger.error(f"Agent [{self.name}] think error: {e}")
            return {"message": {"content": f"思考出错: {e}"}}

    async def _think_stream(self, messages):
        """流式思考模式 — 实时推送 token 给调用方"""
        content = ""
        tool_calls_accumulator = {}

        async for chunk in self.client.stream_chat(messages, self.tool_defs):
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            if delta and delta.content:
                content += delta.content
                if self.on_token:
                    await self.on_token(delta.content)

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
        return {"message": {"content": content or None, "tool_calls": tool_calls}}

    async def _execute_tool(self, name: str, args: Dict) -> str:
        try:
            if name == "subagent" and self.subagent_manager:
                return await self._execute_subagent(args)

            if self.tool_registry and self.tool_registry.has_tool(name):
                return await self.tool_registry.execute(name, args)

            if self.skill_manager and name == "execute_skill":
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
                            return await plugin.execute_tool(name, args)

            return f"工具 {name} 不存在"
        except Exception as e:
            return f"工具执行错误: {e}"

    async def _execute_subagent(self, args: Dict) -> str:
        task = args.get("task")
        if not task:
            return json.dumps({"success": False, "error": "缺少task参数"}, ensure_ascii=False)

        try:
            result = await self.subagent_manager.run_subagent(
                task=task,
                template=args.get("template", ""),
                name=args.get("name", ""),
                session_id=args.get("session_id", ""),
                system_prompt=args.get("system_prompt", ""),
                tools=args.get("tools"),
                mcp_servers=args.get("mcp_servers"),
                client=self.client,
                parent_agent=self,
                keep_alive=args.get("keep_alive", True)
            )
        except Exception as e:
            logger.error(f"Subagent execution error: {e}")
            return json.dumps({"success": False, "error": f"子代理执行错误: {e}"}, ensure_ascii=False)

        stats = self.subagent_manager.get_stats()

        return json.dumps({
            "success": result.status == "completed",
            "agent_id": result.agent_id,
            "status": result.status,
            "result": result.result,
            "active_subagents": stats["active_count"]
        }, ensure_ascii=False)

    async def cleanup(self):
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        if self.session_manager:
            self.session_manager.stop_cleanup_task()

        if not self.parent_agent and self.subagent_manager:
            self.subagent_manager.stop_cleanup_task()
            await self.subagent_manager.cleanup_all()

        if self.memory and not self.parent_agent:
            self.learner.stop_daily_task()
        if self.mcp:
            await self.mcp.close()
        logger.info(f"Agent [{self.name}] cleaned up")
