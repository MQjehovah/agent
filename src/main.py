import asyncio
import contextlib
import logging
import os
import sys
import warnings
from pathlib import Path

from dotenv import load_dotenv

from bootstrap import bootstrap, cleanup
from cli import parse_args

logger = logging.getLogger("agent.main")

# 日志目录必须在模块导入前设置（llm.py 在导入时读取 AGENT_LOG_DIR）
_LOCAL_LOG = os.path.join(Path(__file__).parent.parent, "logs")
_AGENT_LOG = os.path.join(os.path.expanduser("~"), "agent", "logs")
os.environ.setdefault("AGENT_LOG_DIR", _LOCAL_LOG if os.path.isdir(_LOCAL_LOG) else _AGENT_LOG)

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
warnings.filterwarnings("ignore", category=ResourceWarning, message=".*unclosed.*transport.*")
warnings.filterwarnings("ignore", category=ResourceWarning, message=".*unclosed transport.*")
_orig_unraisable = getattr(sys, "unraisablehook", None)


def _silent_hook(hook_args):
    msg = str(hook_args.exc_value) if hook_args.exc_value else ""
    if "Event loop is closed" in msg or "I/O operation on closed pipe" in msg:
        return
    if _orig_unraisable:
        _orig_unraisable(hook_args)


sys.unraisablehook = _silent_hook


async def main():
    args = parse_args()
    agent, plugin_manager, web_server, target_agent = await bootstrap(args)
    shutdown_event = asyncio.Event()

    try:
        if args.mode == "autonomous":
            from autonomous.runner import autonomous_mode
            await autonomous_mode(agent, shutdown_event, args)
        else:
            from interactive import interactive_mode
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
        for t in asyncio.all_tasks(loop):
            t.cancel()
        with contextlib.suppress(BaseException):
            loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
