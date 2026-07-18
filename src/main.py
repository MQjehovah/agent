import asyncio
import contextlib
import gc
import logging
import os
import signal
import sys
import time
import uuid
import warnings
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from agent.core import Agent
from agent.session import AgentSessionManager
from cmd_handler import CommandHandler
from config import Config, validate_config
from llm.client import LLMClient
from plugins import PluginManager
from settings import get_settings, init_settings
from tui import TUIApp
from tui.display import _fmt_args, _truncate
from tui.styles import BOLD, CYAN, DIM, GRAY, GREEN, RED, RESET, YELLOW

# 日志目录必须在模块导入前设置（llm.py 在导入时读取 AGENT_LOG_DIR）
_LOCAL_LOG = os.path.join(Path(__file__).parent.parent, "logs")
_AGENT_LOG = os.path.join(os.path.expanduser("~"), "agent", "logs")
os.environ.setdefault("AGENT_LOG_DIR", _LOCAL_LOG if os.path.isdir(_LOCAL_LOG) else _AGENT_LOG)


console = Console()

# 加载环境配置
_project_root = Path(__file__).parent.parent
_env_file = _project_root / ".env"
if _env_file.exists():
    load_dotenv(_env_file)
else:
    _env_example = _project_root / ".env.example"
    if _env_example.exists():
        load_dotenv(_env_example)

os.environ["PYTHONIOENCODING"] = "utf-8"

# 抑制 Windows asyncio 关闭时的管道清理和子进程传输警告
warnings.filterwarnings("ignore", category=ResourceWarning,
                        message=".*unclosed.*transport.*")
warnings.filterwarnings("ignore", category=ResourceWarning,
                        message=".*unclosed transport.*")
_orig_unraisable = getattr(sys, "unraisablehook", None)
def _silent_hook(hook_args):
    msg = str(hook_args.exc_value) if hook_args.exc_value else ""
    if "Event loop is closed" in msg or "I/O operation on closed pipe" in msg:
        return
    if _orig_unraisable:
        _orig_unraisable(hook_args)
sys.unraisablehook = _silent_hook


# ── 文件日志（不输出到终端，路径在 main 中解析后初始化） ─────────
_LOGGER_INITIALIZED = False
logger: logging.Logger = None


def _init_logging(log_dir: str):
    """初始化文件日志（在路径解析后调用）"""
    global _LOGGER_INITIALIZED, logger
    if _LOGGER_INITIALIZED:
        return
    _LOGGER_INITIALIZED = True
    os.makedirs(log_dir, exist_ok=True)
    _log_file = os.path.join(
        log_dir, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
    root = logging.getLogger()
    if root.hasHandlers():
        root.handlers.clear()
    file_handler = logging.FileHandler(_log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)

    for noisy in ("mcp.server.lowlevel.server", "httpx", "apscheduler.scheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logger = logging.getLogger("agent.main")
    logger.info("日志系统初始化完成，目录: %s", log_dir)


# 绑定到 CLI 会话的插件会话（如 feishu 绑定后共享上下文）
BOUND_PLUGIN_SESSION: str = ""


async def interactive_mode(agent: Agent, shutdown_event: asyncio.Event, target_agent: str = ""):
    """交互模式 — target_agent 不为空时直接路由到子代理"""
    from channels import MessageRouter
    router = MessageRouter(agent)
    session_id = router.format_session_id("cli", uuid.uuid4().hex[:12])

    # 工作目录上下文
    ws_context = os.path.basename(os.path.normpath(agent.workspace))
    branch = ""
    try:
        import subprocess as _sp
        branch = _sp.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                  cwd=agent.workspace, stderr=_sp.DEVNULL, timeout=3).decode().strip()
    except Exception:
        pass
    if target_agent:
        ws_context += f" → {target_agent}"

    # ── TUI ───────────────────────────────────────────────────
    tui = TUIApp(agent)
    tui.setup(ws_context, branch, target_agent, session_id)
    tui.register_hooks(agent)

    ask_tool = agent.tool_registry.get_tool(
        "ask_user") if agent.tool_registry else None
    tui.setup_ask_handler(ask_tool)

    cmd_handler = CommandHandler(agent, session_id, on_exit=shutdown_event.set,
                                  output=tui.chat.append_output)
    current_task: asyncio.Task | None = None

    # 启动 TUI（清屏 + 全屏 Application）
    await tui.start()

    # ── 进度回调（用于 team agent 模式） ──
    def _team_progress(stage, status, info, extra=None):
        now = time.time()
        if status == "start":
            tui.state.current_stage = stage
            tui.state.agent_name = info
            tui.chat.append_output(f"  {DIM}{'─' * 40}{RESET}")
            tui.chat.append_output(f"  {BOLD}{CYAN}{stage}{RESET}  {GRAY}({info}){RESET}")
        elif status == "pipeline":
            tui.chat.append_output(f"  {DIM}pipeline: {', '.join(info)}{RESET}")
        elif status == "feedback":
            tui.chat.append_output(f"  {DIM}{'─' * 40}{RESET}")
            tui.chat.append_output(f"  {BOLD}{YELLOW}↻{RESET} 开发↔测试反馈循环 {GRAY}{info}{RESET}")
            if extra:
                tui.chat.append_output(f"  {DIM}  · 失败详情: {GRAY}{extra[:120]}{RESET}")
        elif status == "stage_timeout":
            tui.chat.append_output(f"  {DIM}  {YELLOW}⚠{RESET} {GRAY}timeout{RESET}")
        elif status == "stage_done":
            parts = stage.split("|", 1)
            name = parts[0]
            tui.state.current_stage = ""
            tui.chat.append_output(f"  {DIM}  {GREEN}✔{RESET} {name}  {GRAY}{now - tui.state.task_start_ts:.0f}s{RESET}")
        elif status == "llm":
            text = str(info or "").strip()
            if text:
                first = text.split("\n")[0].strip()[:120]
                if first:
                    tui.chat.append_output(f"  {DIM}  · {GRAY}{first}{RESET}")
        elif status == "tool_start":
            tname = stage.split("|", 1)[0] if "|" in stage else stage
            brief = _fmt_args(info) if info else ""
            tui.chat.append_output(f"  {DIM}  · {tname} {GRAY}{brief}{RESET}")
        elif status == "tool_result":
            brief = _truncate(extra or "", 45)
            if brief and (brief.startswith('{"success": false') or "错误" in brief or "失败" in brief):
                tui.chat.append_output(f"  {DIM}  · {RED}✗{RESET} {DIM}{brief[:60]}{RESET}")

    # ── 信号处理 ──────────────────────────────────────────────
    def handle_signal():
        shutdown_event.set()
        tui.shutdown()
        if current_task and not current_task.done():
            current_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, handle_signal)

    # ── 主循环 ────────────────────────────────────────────────
    try:
        while not shutdown_event.is_set() and not tui.is_shutdown:
            question = await tui.get_input()
            if question is None:
                break

            if not question.strip():
                continue

            if cmd_handler.is_command(question):
                await cmd_handler.handle(question)
                continue

            task_counter = 0 if not hasattr(interactive_mode, '_counter') else interactive_mode._counter
            task_counter += 1
            interactive_mode._counter = task_counter

            async def _run_task(task_id: int, question: str):
                nonlocal current_task
                cmd_handler.set_current_task_id(task_id)
                tui.start_task()
                tui.start_spinner()

                try:
                    target = target_agent
                    if target and agent.subagent_manager:
                        if target in agent.subagent_manager._team_configs:
                            team_dir = os.path.join(
                                agent.config_dir, "agents", target)
                            team_agent = Agent(
                                workspace=agent.workspace, config_dir=team_dir,
                                client=agent.client, parent_agent=agent,
                                permission_mode=getattr(agent, '_permission_config', None) and
                                agent._permission_config.mode.value or "auto",
                            )
                            team_agent.subagent_manager = agent.subagent_manager
                            team_agent._progress_callback = _team_progress
                            await team_agent.initialize()
                            result = await team_agent.run(
                                question, session_id=session_id,
                                user_id="cli:admin", user_name="管理员",
                            )
                        else:
                            instance, _ = await agent.subagent_manager.get_or_create_subagent(
                                name=target, session_id=session_id,
                                client=agent.client, parent_agent=agent,
                            )
                            result = await instance.agent.run(question)
                    else:
                        result = await agent.run(question, session_id=session_id,
                                                  user_id="cli:admin", user_name="管理员")
                    await tui.stop_spinner()
                    text = result.result if hasattr(result, "result") else str(result)
                    tui.after_task(text)
                except asyncio.CancelledError:
                    await tui.stop_spinner()
                    logger.warning("任务被用户取消")
                    tui.cancel_notice()
                except Exception as e:
                    await tui.stop_spinner()
                    logger.exception(f"任务执行异常: {e}")
                    tui.error_notice(str(e))
                finally:
                    cmd_handler.set_current_task_id(None)

            current_task = asyncio.create_task(
                _run_task(task_counter, question))

            # 等待任务完成，期间检查取消信号
            while not current_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(current_task), timeout=0.3)
                    break
                except asyncio.TimeoutError:
                    if tui.cancel_flag.is_set() and not current_task.done():
                        current_task.cancel()
                        break
                    if shutdown_event.is_set():
                        if not current_task.done():
                            current_task.cancel()
                        break
                    continue

            current_task = None

    finally:
        tui.shutdown()
        if current_task and not current_task.done():
            current_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await current_task


async def autonomous_mode(agent: Agent, shutdown_event: asyncio.Event, args):
    """自主模式 - 感知-规划-执行-校验循环"""
    from autonomous.eventbus import EventBus
    from autonomous.executor import Executor
    from autonomous.goal import GoalManager
    from autonomous.loop import AutonomousLoop
    from autonomous.perceiver import Perceiver
    from autonomous.planner import Planner
    from autonomous.reporter import DingTalkReporter, Reporter
    from autonomous.verifier import Verifier
    from storage.storage import get_storage
    storage = get_storage()

    event_bus = EventBus(storage=storage)
    goal_manager = GoalManager(storage=storage)

    kanban_board = None
    if agent.plugin_manager:
        kp = agent.plugin_manager.get_plugin("kanban")
        if kp:
            kanban_board = kp.get_board()

    tool_summary = ""
    if hasattr(agent, "_get_tool_summary"):
        tool_summary = agent._get_tool_summary()

    subagent_summary = ""
    if agent.subagent_manager:
        subagent_summary = agent.subagent_manager.get_subagent_prompt()

    perceiver = Perceiver(event_bus=event_bus, agent=agent)
    planner = Planner(
        client=agent.client,
        tool_summary=tool_summary,
        subagent_summary=subagent_summary,
    )

    dingtalk_plugin = None
    plugin_manager = agent.plugin_manager

    if plugin_manager:
        dingtalk_plugin = plugin_manager.get_plugin("dingtalk")

        scheduler_plugin = plugin_manager.get_plugin("scheduler")
        if scheduler_plugin:
            async def _schedule_to_perceiver(schedule_task: str):
                await perceiver.handle_schedule({"name": "定时任务", "task": schedule_task})
            scheduler_plugin._agent_executor = _schedule_to_perceiver
            scheduler_plugin.start()

    if (
        dingtalk_plugin
        and hasattr(dingtalk_plugin, "sessions")
        and dingtalk_plugin.sessions
    ):
        reporter = DingTalkReporter(dingtalk_plugin=dingtalk_plugin)
    else:
        reporter = Reporter()

    executor = Executor(agent=agent, reporter=reporter)
    verifier = Verifier(client=agent.client)

    auto_loop = AutonomousLoop(
        event_bus=event_bus,
        agent=agent,
        goal_manager=goal_manager,
        planner=planner,
        executor=executor,
        verifier=verifier,
        reporter=reporter,
        perceiver=perceiver,
        board=kanban_board,
        shutdown_event=shutdown_event,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)

    board_info = ""
    if kanban_board:
        stats = kanban_board.get_stats()
        board_info = f"看板: {stats['total']} 个任务 ({stats['by_column']})"

    console.print(
        Panel.fit(
            "[bold green]自主模式已启动[/bold green]\n"
            f"目标数据库: {storage.db_path}\n"
            f"{board_info}\n"
            "信号源: 钉钉消息 | Webhook | 定时任务 | 看板\n"
            "等待事件...",
            border_style="green",
        )
    )

    await auto_loop.run()
    return plugin_manager


async def cleanup(plugin_manager, agent):
    """统一清理资源"""
    try:
        if plugin_manager:
            plugin_manager.stop_all()
        await agent.cleanup()
    except asyncio.CancelledError:
        logger.warning("清理过程被取消")
    except Exception as e:
        logger.error(f"清理过程出错: {e}")

    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if tasks:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)

    # 关闭事件循环前执行一次 GC，让 subprocess transport 在循环还活着时被回收
    gc.collect()


async def main():
    shutdown_event = asyncio.Event()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", default="config",
                        help="配置目录，包含PROMPT.md、agents/、skills/等 (默认: ./config)")
    parser.add_argument("--workspace", "-w", default=".",
                        help="工作目录，agent 在此目录下读写文件 (默认: 当前目录)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-plugins", action="store_true")
    parser.add_argument("--skip-config-check", action="store_true")
    parser.add_argument(
        "--mode",
        "-m",
        choices=["interactive", "autonomous"],
        default="interactive",
        help="运行模式",
    )
    parser.add_argument(
        "--agent",
        "-a",
        default="",
        help="指定子代理名称执行任务",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动Web UI前端",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web UI端口 (默认8080)",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="禁用Web UI前端",
    )
    parser.add_argument(
        "task",
        nargs="*",
        help="要执行的任务内容",
    )
    args = parser.parse_args()

    # ── 路径解析：当前目录 → 用户目录/agent → 自动创建 ──────────
    _user_agent_dir = os.path.join(os.path.expanduser("~"), "agent")

    def _resolve_path(path: str, name: str) -> str:
        """按优先级解析路径: 当前目录 > 用户目录/agent > 自动创建"""
        if os.path.exists(path):
            return os.path.abspath(path)
        user_path = os.path.join(_user_agent_dir, name)
        if os.path.exists(user_path):
            return os.path.abspath(user_path)
        # 自动创建并复制默认配置（打包环境下从 sys._MEIPASS 复制）
        os.makedirs(user_path, exist_ok=True)
        _meipass = getattr(sys, '_MEIPASS', None)
        if _meipass:
            _src = os.path.join(_meipass, name)  # 打包：config/ 在 _MEIPASS 下
        else:
            _src = os.path.join(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))), name)
        if os.path.exists(_src):
            import shutil
            for item in os.listdir(_src):
                s = os.path.join(_src, item)
                d = os.path.join(user_path, item)
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
        return os.path.abspath(user_path)

    config_dir = _resolve_path(args.config, "config")
    workspace = _resolve_path(args.workspace, "workspace")

    # 日志目录
    _log_dir = os.environ["AGENT_LOG_DIR"]
    _init_logging(_log_dir)

    init_settings(config_dir)

    Config.load_from_env()
    AgentSessionManager.load_config()

    # 应用日志等级（Settings 已加载，覆盖 basicConfig 的默认 INFO）
    logging.getLogger().setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
    logging.getLogger("agent").setLevel(
        getattr(logging, Config.LOG_LEVEL, logging.INFO))

    if not args.skip_config_check and not validate_config():
        console.print("[red]配置验证失败[/red]")
        return

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    src_dir = os.path.dirname(os.path.abspath(__file__))

    client = LLMClient(
        endpoints=get_settings().llm_endpoints,
        timeout=get_settings().llm_timeout,
        connect_timeout=get_settings().llm_connect_timeout,
    )
    agent = Agent(workspace=workspace, config_dir=config_dir, client=client)
    await agent.initialize()

    target_agent = args.agent or ""

    if target_agent:
        _team_dir = os.path.join(config_dir, "agents", target_agent)
        # 替换为团队自身的 system prompt（config/agents/团队名/PROMPT.md）
        _team_prompt = os.path.join(_team_dir, "PROMPT.md")
        if os.path.exists(_team_prompt):
            from utils.frontmatter import extract_frontmatter
            with open(_team_prompt, encoding="utf-8") as _f:
                _content = _f.read()
            _fm, _body = extract_frontmatter(_content)
            if _body:
                agent.name = _fm.get("name", target_agent) if isinstance(
                    _fm, dict) else target_agent
                agent.description = _fm.get(
                    "description", "") if isinstance(_fm, dict) else ""
                agent.system_prompt = agent.system_prompt_raw = _body
                # 确保根 agent 有团队技能
                _team_skills_dir = os.path.join(_team_dir, "skills")
                if os.path.exists(_team_skills_dir):
                    from skills import SkillManager
                    if not agent.skill_manager:
                        agent.skill_manager = SkillManager(_team_skills_dir)
                    else:
                        _tsm = SkillManager(_team_skills_dir)
                        for _sn in _tsm.list_skills():
                            if _sn not in agent.skill_manager.skills:
                                _sk = _tsm.get_skill(_sn)
                                if _sk:
                                    agent.skill_manager.skills[_sn] = _sk
                    agent.skill_manager._build_builtin_tools()
                    _skill_names = agent.skill_manager.list_skills()
                    if _skill_names:
                        _skill_guide = (
                            "\n\n## 技能工具\n"
                            "你有一个 `skill` 工具，可以加载结构化的工作流指引。\n"
                            "执行任务前，先判断是否有适用于当前工作阶段的 skill，如果有则优先调用 `skill` 工具加载。\n"
                            f"可用技能: {', '.join(_skill_names)}"
                        )
                        agent.system_prompt += _skill_guide
                        agent.system_prompt_raw += _skill_guide
                # 用团队成员替换子代理列表
                if agent.subagent_manager:
                    _members = agent.subagent_manager._team_members.get(
                        target_agent, {})
                    if _members:
                        _orig_prompt = agent.subagent_manager.get_subagent_prompt
                        _lines = ["\n\n## 【团队成员】\n"]
                        for _mname, _minfo in _members.items():
                            _lines.append(f"名称：{_mname}\n")
                            _lines.append(
                                f"角色：{_minfo.get('description', '')}\n")
                        _lines.append("\n团队 Leader 可根据需要将任务委派给对应成员。")
                        _team_prompt_text = "\n".join(_lines)
                        # 注入到 prompt builder 中
                        agent.subagent_manager.get_subagent_prompt = lambda: _team_prompt_text
                agent._build_prompt()

    web_server = None
    plugin_manager = None

    try:
        from channels import MessageRouter
        router = MessageRouter(agent)

        start_web = args.web

        if not args.no_plugins:
            plugin_manager = PluginManager(os.path.join(
                src_dir, "plugins"), config_dir=config_dir)
            plugin_manager.load_all()
            plugin_manager.router = router

            async def _plugin_exec(sid, c, uid="", uname=""):
                if BOUND_PLUGIN_SESSION:
                    bsid = BOUND_PLUGIN_SESSION
                    uid = "cli:admin"
                    uname = "管理员(绑定)"
                else:
                    bsid = sid
                r = await router.route(c, channel="plugin", session_id=bsid, user_id=uid, user_name=uname)
                return r.result if hasattr(r, 'result') else str(r)
            plugin_manager.register_executor(_plugin_exec)
            agent.plugin_manager = plugin_manager

            kanban_plugin = plugin_manager.get_plugin("kanban")
            if kanban_plugin:
                kanban_plugin.set_agent(agent)

            plugin_manager.start_all()

            webhook_plugin = plugin_manager.get_plugin("webhook")
            if webhook_plugin:
                pass  # webhook 现在直接使用 plugin_manager.router

            scheduler_plugin = plugin_manager.get_plugin("scheduler")
            if scheduler_plugin:
                scheduler_plugin._agent_executor = lambda task: router.route(
                    task, channel="scheduler",
                )
                if not scheduler_plugin._started:
                    scheduler_plugin.start()

        kanban_board = None
        if agent.plugin_manager:
            kp = agent.plugin_manager.get_plugin("kanban")
            if kp:
                kanban_board = kp.get_board()

        if start_web:
            from web import WebServer
            web_server = WebServer(
                port=args.web_port, loop=asyncio.get_running_loop())
            web_server.set_agent(agent)
            if kanban_board:
                web_server.set_kanban(kanban_board)
            web_server.start()

        if args.mode == "autonomous":
            await autonomous_mode(agent, shutdown_event, args)
        else:
            await interactive_mode(agent, shutdown_event, target_agent)
    except asyncio.CancelledError:
        logger.info("任务取消")
    except Exception as e:
        logger.error(f"程序异常退出: {e}", exc_info=True)
    finally:
        logger.info("清理资源...")
        if web_server:
            web_server.stop()
        await cleanup(plugin_manager, agent)
        logger.info("清理完成")


if __name__ == "__main__":
    # 捕获 anyio cancel scope 跨任务 bug（MCP stdio 引起），不污染终端
    _log = logging.getLogger()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.set_exception_handler(lambda _loop, ctx: _log.log(
        logging.DEBUG if "cancel scope" in ctx.get("message", "") else logging.WARNING,
        "事件循环异常: %s", ctx.get("message", ""),
    ))
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
    finally:
        # 清理所有剩余任务，避免 anyio cancel scope 崩溃
        for t in asyncio.all_tasks(loop):
            t.cancel()
        with contextlib.suppress(BaseException):
            loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
