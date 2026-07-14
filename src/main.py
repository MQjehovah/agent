import asyncio
import gc
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

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

from agent import Agent
from agent_session import AgentSessionManager
from cmd_handler import CommandHandler
from config import Config, validate_config
from hooks import HookEvent
from llm import LLMClient
from plugins import PluginManager
from settings import get_settings, init_settings

# ── 文件日志（不输出到终端） ──────────────────────────────────────────
LOG_DIR = os.path.join(_project_root, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_log_file = os.path.join(LOG_DIR, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
file_handler = logging.FileHandler(_log_file, encoding="utf-8")
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logging.basicConfig(level=logging.INFO, handlers=[file_handler])

# 抑制第三方库日志噪音
for noisy in ("mcp.server.lowlevel.server", "httpx", "apscheduler.scheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("agent.main")

# ── ANSI ────────────────────────────────────────────────────────────
_SGR = lambda c: f"\033[{c}m" if sys.stdout.isatty() else ""
_DIM = _SGR("2")
_GREEN = _SGR("32")
_YELLOW = _SGR("33")
_CYAN = _SGR("36")
_RED = _SGR("31")
_RESET = _SGR("0")
_BOLD = _SGR("1")
_GRAY = _SGR("90")


def _truncate(text: str, w: int = 60) -> str:
    if not text:
        return ""
    line = text.split("\n")[0].strip()
    if len(line) > w:
        return line[: w - 3] + "..."
    return line


def _fmt_args(args: dict) -> str:
    """从工具参数中提取人类可读的摘要"""
    if not args:
        return ""
    for key in ("path", "file_path", "pattern", "name", "command"):
        val = args.get(key)
        if val:
            return str(val)[:60]
    return _truncate(json.dumps(args, ensure_ascii=False), 60)


def _write(text: str = "", end: str = "\n"):
    sys.stdout.write(text + end)
    sys.stdout.flush()


def _clear_line():
    _write("\r\033[K", end="")


# ── Terminal UI ─────────────────────────────────────────────────────
_STATE = {"task_done": False, "task_start_ts": 0.0, "tool_count": 0, "round": 0, "subagent_depth": 0}


def _elapsed() -> str:
    t = time.time() - _STATE["task_start_ts"]
    if t < 60:
        return f"{t:.0f}s"
    return f"{t//60:.0f}m{t%60:.0f}s"


class TerminalUI:

    def __init__(self):
        self._spinner_idx = 0
        self._chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._active = False

    # ── prompts ──

    def say(self, text: str):
        """普通信息行"""
        _write(f"  {text}")

    def dim(self, text: str):
        _write(f"  {_DIM}{text}{_RESET}")

    def ok(self, text: str):
        _write(f"  {_GREEN}{text}{_RESET}")

    def warn(self, text: str):
        _write(f"  {_YELLOW}{text}{_RESET}")

    def err(self, text: str):
        _write(f"  {_RED}{text}{_RESET}")

    def rule(self):
        """分隔线"""
        _write(f"  {_DIM}{'─' * 60}{_RESET}")

    def prompt(self, context: str = ""):
        """显示输入提示"""
        prefix = f"{_GREEN}❯{_RESET}" if sys.stdout.isatty() else ">"
        ctx = f" {_DIM}{context}{_RESET}" if context else ""
        _write(f"\n{prefix}{ctx} ", end="")

    def prompt_continue(self):
        """多行继续提示"""
        _write(f"  {_DIM}...{_RESET} ", end="")

    # ── task lifecycle ──

    def task_start(self):
        _STATE["task_done"] = False
        _STATE["task_start_ts"] = time.time()
        _STATE["tool_count"] = 0
        _STATE["round"] = 0
        _STATE["subagent_depth"] = 0
        self._active = True
        self._spinner_idx = 0

    def task_done(self):
        self._active = False
        _STATE["task_done"] = True

    def round_start(self, iteration: int):
        _STATE["round"] = iteration
        if iteration > 1:
            _clear_line()

    def tick(self):
        if not self._active:
            return
        ch = self._chars[self._spinner_idx % len(self._chars)]
        self._spinner_idx += 1
        elapsed = _elapsed()
        r = _STATE["round"]
        t = _STATE["tool_count"]
        status = f"thinking" if r == 0 else f"round {r}"
        extra = f" · {t} tools" if t else ""
        _write(f"\r  {_DIM}{ch} {status}{extra}  {elapsed}{_RESET}", end="")

    # ── tool calls ──

    def tool_call(self, name: str, args: dict):
        brief = _fmt_args(args)
        _STATE["tool_count"] += 1
        _clear_line()
        icon = "▶"
        if name in ("read", "file_operation"):
            icon = "📖" if sys.stdout.isatty() else "·"
        elif name in ("edit", "write"):
            icon = "✏️" if sys.stdout.isatty() else "·"
        elif name == "shell":
            icon = "⚡" if sys.stdout.isatty() else "·"
        elif name == "subagent":
            _STATE["subagent_depth"] += 1
            icon = "⊕" if sys.stdout.isatty() else "+"
        _write(f"  {_DIM}{icon} {name}{_RESET} {_GRAY}{brief}{_RESET}")

    def tool_result(self, name: str, result: str):
        brief = _truncate(result, 60)
        if not brief or brief == "{}" or brief.startswith('{"success": true'):
            return
        if brief.startswith('{"success": false'):
            _write(f"  {_RED}✗ {name} → {brief[:80]}{_RESET}")
            return
        _write(f"  {_GREEN}✔{_RESET} {_DIM}{brief}{_RESET}")

    def subagent_result(self, name: str, status: str):
        _STATE["subagent_depth"] -= 1
        s = f"{_GREEN}done{_RESET}" if status == "completed" else f"{_RED}{status}{_RESET}"
        _write(f"  {_DIM}└─ {name} [{s}]{_RESET}")

    # ── output ──

    def result_text(self, text: str):
        if not text:
            return
        elapsed = _elapsed()
        _write(f"  {_DIM}{'─' * 60}{_RESET}")
        in_code = False
        for line in text.strip().split("\n"):
            if line.startswith("```"):
                if in_code:
                    _write(f"  {_DIM}```{_RESET}")
                else:
                    lang = line[3:].strip()
                    _write(f"  {_DIM}```{lang}{_RESET}")
                in_code = not in_code
                continue
            if in_code:
                _write(f"  {_GRAY}{line}{_RESET}")
            elif line.strip():
                _write(f"  {line}")
            else:
                _write("")
        _write(f"  {_DIM}{'─' * 60}{_RESET}")
        _write(f"  {_DIM}completed in {elapsed}{_RESET}")

    def thinking(self, content: str):
        """显示推理过程（可选）"""
        if not content:
            return
        _clear_line()
        for line in content.strip().split("\n")[-3:]:
            t = _truncate(line, 80)
            if t:
                _write(f"  {_DIM}┊ {t}{_RESET}")


async def interactive_mode(agent: Agent, shutdown_event: asyncio.Event):
    """交互模式"""
    session_id = str(uuid.uuid4())
    current_task: asyncio.Task | None = None
    task_counter = 0
    input_queue: asyncio.Queue[str] = asyncio.Queue()
    input_task: asyncio.Task | None = None
    ui = TerminalUI()

    # 获取工作目录上下文
    ws_context = os.path.basename(os.path.normpath(agent.workspace))
    branch = ""
    try:
        import subprocess as _sp
        branch = _sp.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                   cwd=agent.workspace, stderr=_sp.DEVNULL, timeout=3).decode().strip()
    except Exception:
        pass
    ctx_str = f"{ws_context}" + (f" {_DIM}({branch}){_RESET}" if branch else "")

    # 注册钩子
    def on_tool_start(ctx):
        ui.tool_call(ctx.tool_name, ctx.arguments or {})
    def on_tool_result(ctx):
        ui.tool_result(ctx.tool_name, str(ctx.result or ""))
    def on_round_start(ctx):
        it = (ctx.metadata or {}).get("iteration", 0)
        ui.round_start(it)
    def on_subagent_result(ctx):
        meta = ctx.metadata or {}
        ui.subagent_result(meta.get("name", "?"), meta.get("status", "?"))
    def on_reasoning(ctx):
        if hasattr(ctx, "token") and ctx.token:
            pass  # streaming tokens — could show thinking

    agent.hooks.register(HookEvent.TOOL_START, on_tool_start)
    agent.hooks.register(HookEvent.TOOL_RESULT, on_tool_result)
    agent.hooks.register(HookEvent.ROUND_START, on_round_start)
    agent.hooks.register(HookEvent.SUBAGENT_RESULT, on_subagent_result)

    # 设置 ask_user 输入处理器（避免与 input_reader 冲突）
    ask_tool = agent.tool_registry.get_tool("ask_user") if agent.tool_registry else None
    if ask_tool and hasattr(ask_tool, "set_input_handler"):
        async def _on_ask_user(question: str, options: list, default: str) -> str:
            _STATE["task_done"] = True  # 暂停 spinner
            _input_paused.set()         # 暂停背景输入读取
            _clear_line()
            if options:
                _write(f"  {_BOLD}{question}{_RESET}")
                for i, opt in enumerate(options, 1):
                    _write(f"  {_DIM}{i}.{_RESET} {opt}")
                prompt = f"  {_GREEN}❯{_RESET} " + (f"({default}) " if default else "")
            else:
                _write(f"  {_BOLD}{question}{_RESET}")
                prompt = f"  {_GREEN}❯{_RESET} " + (f"({default}) " if default else "")
            try:
                loop = asyncio.get_running_loop()
                raw = await loop.run_in_executor(None, lambda: input(prompt))
                if not raw:
                    return default or ""
                if options and raw.isdigit() and 1 <= int(raw) <= len(options):
                    return options[int(raw) - 1]
                return raw
            finally:
                _input_paused.clear()   # 恢复背景输入读取
        ask_tool.set_input_handler(_on_ask_user)

    cmd_handler = CommandHandler(agent, session_id, on_exit=shutdown_event.set)

    async def _spinner():
        while not shutdown_event.is_set() and not _STATE["task_done"]:
            ui.tick()
            await asyncio.sleep(0.12)
        _clear_line()

    async def run_task(task_id: int, question: str):
        nonlocal current_task
        cmd_handler.set_current_task_id(task_id)
        ui.task_start()
        spinner_task = asyncio.create_task(_spinner())

        try:
            result = await agent.run(question, session_id=session_id,
                                     user_id="cli:admin", user_name="管理员")
            ui.task_done()
            await spinner_task
            ui.result_text(result.result)
        except asyncio.CancelledError:
            ui.task_done()
            await spinner_task
            ui.warn("cancelled")

        cmd_handler.set_current_task_id(None)
        current_task = None

    _stdin_transport = None

    # 用于暂停输入读取（ask_user 期间暂停背景读 stdin）
    _input_paused = asyncio.Event()

    async def input_reader():
        """后台读取用户输入 — Windows 用 msvcrt 字符级读取，Unix 用 StreamReader"""
        nonlocal _stdin_transport
        loop = asyncio.get_event_loop()

        if sys.platform == "win32":
            import msvcrt

            async def _readline():
                line = []
                while not shutdown_event.is_set():
                    if _input_paused.is_set():
                        await asyncio.sleep(0.05)
                        continue
                    ch = await loop.run_in_executor(None, msvcrt.getwch)
                    if ch in ("\r", "\n"):
                        sys.stdout.write("\n")
                        break
                    elif ch in ("\x08", "\x7f"):  # backspace
                        if line:
                            removed = line.pop()
                            if ord(removed) > 0x2000:
                                sys.stdout.write("\b\b  \b\b")
                            else:
                                sys.stdout.write("\b \b")
                    elif ch == "\x03":
                        raise KeyboardInterrupt
                    elif ch == "\x1a":
                        raise EOFError
                    elif ch.isprintable():
                        line.append(ch)
                        sys.stdout.write(ch)
                    sys.stdout.flush()
                return "".join(line)
        else:
            _stdin_reader = asyncio.StreamReader()
            _stdin_protocol = asyncio.StreamReaderProtocol(_stdin_reader)
            _stdin_transport, _ = await loop.connect_read_pipe(
                lambda: _stdin_protocol, sys.stdin
            )

            async def _readline():
                while not shutdown_event.is_set():
                    if _input_paused.is_set():
                        await asyncio.sleep(0.05)
                        continue
                    line = await _stdin_reader.readline()
                    return line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
                return ""

        while not shutdown_event.is_set():
            try:
                ui.prompt(ctx_str)
                first = await _readline()
                if not first:
                    continue

                # 检测多行粘贴：连续两行缩进或 """ 开头
                lines = [first]
                stripped = first.strip()
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    closing = stripped[0:3]
                    if closing not in stripped[3:]:
                        while not shutdown_event.is_set():
                            ui.prompt_continue()
                            nxt = await _readline()
                            if nxt is None:
                                break
                            lines.append(nxt)
                            if closing in nxt:
                                break
                elif first.startswith(" ") or first.startswith("\t"):
                    while not shutdown_event.is_set():
                        chk = await _readline()
                        if chk is None or chk.strip() == "":
                            break
                        lines.append(chk)

                question = "\n".join(lines)
                await input_queue.put(question.strip())
            except (KeyboardInterrupt, EOFError):
                shutdown_event.set()
                break
            except Exception:
                if shutdown_event.is_set():
                    break

    def handle_signal():
        shutdown_event.set()
        if current_task and not current_task.done():
            current_task.cancel()
        if input_task and not input_task.done():
            input_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    input_task = asyncio.create_task(input_reader())

    try:
        while not shutdown_event.is_set():
            try:
                question = await asyncio.wait_for(input_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            if not question.strip():
                continue

            if cmd_handler.is_command(question):
                await cmd_handler.handle(question)
                continue

            task_counter += 1
            current_task = asyncio.create_task(run_task(task_counter, question))

    finally:
        if _stdin_transport is not None:
            _stdin_transport.close()
        if input_task and not input_task.done():
            input_task.cancel()
            try:
                await input_task
            except asyncio.CancelledError:
                pass
        if current_task and not current_task.done():
            current_task.cancel()
            try:
                await current_task
            except asyncio.CancelledError:
                pass


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

    from storage import get_storage
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
        try:
            asyncio.get_running_loop().add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            pass

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
        await asyncio.gather(*tasks, return_exceptions=True)

    # 关闭事件循环前执行一次 GC，让 subprocess transport 在循环还活着时被回收
    import gc
    gc.collect()


async def main():
    shutdown_event = asyncio.Event()

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", "-w", default="workspace",
                        help="agent工作目录，存放agent产生的文件 (默认: ./workspace)")
    parser.add_argument("--config", "-c", default="config",
                        help="配置目录，包含PROMPT.md、agents/、skills/等 (默认: ./config)")
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

    config_dir = os.path.abspath(args.config)
    workspace = os.path.abspath(args.workspace)
    os.makedirs(workspace, exist_ok=True)

    init_settings(config_dir)

    Config.load_from_env()
    AgentSessionManager.load_config()

    # 应用日志等级（Settings 已加载，覆盖 basicConfig 的默认 INFO）
    logging.getLogger().setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))
    logging.getLogger("agent").setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))

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

    async def _agent_spinner(ui):
        while not _STATE["task_done"]:
            ui.tick()
            await asyncio.sleep(0.12)
        _clear_line()

    if args.agent:
        agent_name = args.agent
        task = " ".join(args.task) if args.task else ""
        if not task:
            task = input("请输入任务内容: ")

        ui = TerminalUI()
        ui.say(f"{_BOLD}{agent_name}{_RESET} 正在执行...")

        def _on_progress(stage, status, info, extra=None):
            if status == "start":
                _clear_line()
                ui.dim(f"  {_CYAN}▶{_RESET} {stage}  ({info})")
            elif status == "pipeline":
                stages_str = ", ".join(info)
                ui.dim(f"  流水线: {_DIM}{stages_str}{_RESET}")
            elif status.startswith("tool_"):
                evt = status
                _, name, _stage = stage.split("|", 2)
                if evt == "tool_start":
                    brief = _fmt_args(info) if info else ""
                    _write(f"  {_DIM}  ┊  {_DIM}▶{_RESET} {name} {brief}")
                elif evt == "tool_result":
                    brief = _truncate(extra or "", 50)
                    if brief and not brief.startswith('{"success": true'):
                        _write(f"  {_DIM}  ┊  {_GREEN}✔{_RESET} {_DIM}{brief}{_RESET}")

        if agent.subagent_manager:
            ui.task_start()
            spinner = asyncio.create_task(_agent_spinner(ui))

            result = await agent.subagent_manager.run_subagent(
                task=task, name=agent_name,
                client=agent.client, parent_agent=agent,
                progress_callback=_on_progress,
            )
            ui.task_done()
            await spinner
            ui.rule()
            ui.result_text(result.result if hasattr(result, "result") else str(result))
        else:
            ui.err("子代理管理器未初始化")
        return

    web_server = None
    plugin_manager = None

    try:
        start_web = args.web or (args.mode == "autonomous" and not args.no_web)

        if not args.no_plugins:
            plugin_manager = PluginManager(os.path.join(src_dir, "plugins"), config_dir=config_dir)
            plugin_manager.load_all()
            plugin_manager.register_executor(lambda sid, c, uid="", uname="": agent.run(c, session_id=sid, user_id=uid, user_name=uname))
            agent.plugin_manager = plugin_manager

            kanban_plugin = plugin_manager.get_plugin("kanban")
            if kanban_plugin:
                kanban_plugin.set_agent(agent)

            plugin_manager.start_all()

            webhook_plugin = plugin_manager.get_plugin("webhook")
            if webhook_plugin:
                async def _webhook_exec(sid, c, uid="webhook:admin", uname="Webhook"):
                    from tools.ask_user import set_ask_user_mode, reset_ask_user_mode
                    token = set_ask_user_mode("auto")
                    try:
                        r = await agent.run(c, session_id=sid, user_id=uid, user_name=uname)
                        return r.result if hasattr(r, 'result') else str(r)
                    finally:
                        reset_ask_user_mode(token)

                webhook_plugin.agent_executor = _webhook_exec

            scheduler_plugin = plugin_manager.get_plugin("scheduler")
            if scheduler_plugin:
                scheduler_plugin._agent_executor = agent.run
                if not scheduler_plugin._started:
                    scheduler_plugin.start()

        kanban_board = None
        if agent.plugin_manager:
            kp = agent.plugin_manager.get_plugin("kanban")
            if kp:
                kanban_board = kp.get_board()

        if start_web:
            from web import WebServer
            web_server = WebServer(port=args.web_port, loop=asyncio.get_running_loop())
            web_server.set_agent(agent)
            if kanban_board:
                web_server.set_kanban(kanban_board)
            web_server.start()

        if args.mode == "autonomous":
            await autonomous_mode(agent, shutdown_event, args)
        else:
            await interactive_mode(agent, shutdown_event)
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
    asyncio.run(main())
