import asyncio
import contextlib
import gc
import logging
import os
import shutil
import sys
import warnings
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from agent.core import Agent
from conversation.session import AgentSessionManager
from llm.client import LLMClient
from plugins import PluginManager
from settings import get_settings, init_settings, validate_config

console = Console()

_LOGGER_INITIALIZED = False


def setup_environment(caller_path: str = ""):
    """模块级环境初始化：AGENT_LOG_DIR、.env、警告抑制、编码"""
    caller_path = caller_path or __file__
    _local_log = os.path.join(Path(caller_path).parent.parent, "logs")
    _agent_log = os.path.join(os.path.expanduser("~"), "agent", "logs")
    os.environ.setdefault("AGENT_LOG_DIR", _local_log if os.path.isdir(_local_log) else _agent_log)

    _project_root = Path(caller_path).parent.parent
    _env_file = _project_root / ".env"
    if _env_file.exists():
        load_dotenv(_env_file)
    else:
        _env_example = _project_root / ".env.example"
        if _env_example.exists():
            load_dotenv(_env_example)

    os.environ["PYTHONIOENCODING"] = "utf-8"

    warnings.filterwarnings("ignore", category=ResourceWarning, message=".*unclosed.*transport.*")
    warnings.filterwarnings("ignore", category=ResourceWarning, message=".*unclosed transport.*")
    warnings.filterwarnings("ignore", category=RuntimeWarning, message="coroutine.*was never awaited")

    _orig_unraisable = getattr(sys, "unraisablehook", None)

    def _silent_hook(hook_args):
        msg = str(hook_args.exc_value) if hook_args.exc_value else ""
        if "Event loop is closed" in msg or "I/O operation on closed pipe" in msg:
            return
        if _orig_unraisable:
            _orig_unraisable(hook_args)

    sys.unraisablehook = _silent_hook
logger: logging.Logger = None

# 绑定到 CLI 会话的插件会话（如 feishu 绑定后共享上下文）
BOUND_PLUGIN_SESSION: str = ""


def _init_logging(log_dir: str):
    global _LOGGER_INITIALIZED, logger
    if _LOGGER_INITIALIZED:
        return
    _LOGGER_INITIALIZED = True
    os.makedirs(log_dir, exist_ok=True)
    _log_file = os.path.join(log_dir, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
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


_user_agent_dir = os.path.join(os.path.expanduser("~"), "agent")


def _resolve_path(path: str, name: str) -> str:
    """按优先级解析路径: 当前目录 > 用户目录/agent > 自动创建"""
    if os.path.exists(path):
        return os.path.abspath(path)
    user_path = os.path.join(_user_agent_dir, name)
    if os.path.exists(user_path):
        return os.path.abspath(user_path)
    os.makedirs(user_path, exist_ok=True)
    _meipass = getattr(sys, '_MEIPASS', None)
    if _meipass:
        _src = os.path.join(_meipass, name)
    else:
        _src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    if os.path.exists(_src):
        for item in os.listdir(_src):
            s = os.path.join(_src, item)
            d = os.path.join(user_path, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
    return os.path.abspath(user_path)


def _setup_plugins(agent, config_dir):
    """加载并启动所有插件"""
    src_dir = os.path.dirname(os.path.abspath(__file__))
    from channels import MessageRouter
    router = MessageRouter(agent)

    plugin_manager = PluginManager(os.path.join(src_dir, "plugins"), config_dir=config_dir)
    plugin_manager.load_all()
    plugin_manager.router = router

    async def _plugin_exec(sid, c, uid="", uname=""):
        uid = "cli:admin" if BOUND_PLUGIN_SESSION else (uid or sid or "plugin")
        r = await router.route(c, channel="plugin", user_id=uid)
        return r.result if hasattr(r, 'result') else str(r)

    plugin_manager.register_executor(_plugin_exec)
    agent.plugin_manager = plugin_manager

    kanban_plugin = plugin_manager.get_plugin("kanban")
    if kanban_plugin:
        kanban_plugin.set_agent(agent)

    plugin_manager.start_all()

    scheduler_plugin = plugin_manager.get_plugin("scheduler")
    if scheduler_plugin:
        scheduler_plugin._agent_executor = lambda task: router.route(
            task, channel="scheduler",
        )
        if not scheduler_plugin._started:
            scheduler_plugin.start()

    return plugin_manager


def _setup_web_server(agent, port):
    """启动 Web UI 服务"""
    from web import WebServer
    web_server = WebServer(port=port, loop=asyncio.get_running_loop())
    web_server.set_agent(agent)
    kanban_board = None
    if agent.plugin_manager:
        kp = agent.plugin_manager.get_plugin("kanban")
        if kp:
            kanban_board = kp.get_board()
    if kanban_board:
        web_server.set_kanban(kanban_board)
    web_server.start()
    return web_server


async def bootstrap(args):
    """完整初始化链：路径 → 日志 → settings → agent → plugins → web server"""
    config_dir = _resolve_path(args.config, "config")
    workspace = _resolve_path(args.workspace, "workspace")

    _log_dir = os.environ["AGENT_LOG_DIR"]
    _init_logging(_log_dir)

    init_settings(config_dir)
    AgentSessionManager.load_config()

    log_level = get_settings().get("logging.level", "INFO")
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))
    logging.getLogger("agent").setLevel(getattr(logging, log_level, logging.INFO))

    if not args.skip_config_check and not validate_config():
        console.print("[red]配置验证失败[/red]")
        sys.exit(1)

    if args.debug:
        logging.getLogger("agent").setLevel(logging.DEBUG)

    client = LLMClient(
        endpoints=get_settings().llm_endpoints,
        timeout=get_settings().llm_timeout,
        connect_timeout=get_settings().llm_connect_timeout,
    )
    agent = Agent(workspace=workspace, config_dir=config_dir, client=client)
    await agent.initialize()

    agent.factory.__class__._instance = agent.factory
    from channels import MessageRouter
    MessageRouter._instance = MessageRouter(agent_name=args.agent or "")

    plugin_manager = None
    if not args.no_plugins:
        plugin_manager = _setup_plugins(agent, config_dir)

    web_server = None
    start_web = args.web
    if start_web:
        web_server = _setup_web_server(agent, args.web_port)

    return plugin_manager, web_server


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

    gc.collect()
