import asyncio
import contextlib
import logging
from io import StringIO

from agent.factory import AgentFactory
from channels import MessageRouter
from commands.handler import CommandHandler, strip_ansi
from tools.ask_user import reset_ask_user_mode, set_ask_user_mode

logger = logging.getLogger("agent.main")


def _default_role(user_id: str) -> str:
    return "admin" if user_id in ("1", "cli:1", "admin") else "default"


async def interactive_mode():
    """交互模式主循环：poll → 命令拦截 → agent.run() → respond

    同时启动 CLI 通道作为生产者。
    """
    from channels.cli import run as cli_run
    cli_task = asyncio.create_task(cli_run())

    router = MessageRouter.instance()
    try:
        while True:
            content, channel, user_id, run_id = await router.poll()
            try:
                session_id = router.format_session_id(channel, user_id)

                agent = await AgentFactory.instance().get_or_create(router.agent_name)
                agent.session_manager.create_session(
                    session_id, user_id=user_id, role=_default_role(user_id),
                    system_prompt=agent.system_prompt or "",
                    agent_id=agent.agent_id,
                )

                buf = StringIO()
                cmd_handler = CommandHandler(agent, session_id,
                                              output=lambda t, _b=buf: _b.write(strip_ansi(t)) if t else None)
                if cmd_handler.is_command(content):
                    await cmd_handler.handle(content)
                    router.respond(channel, user_id, buf.getvalue().strip())
                    continue

                if channel != "cli":
                    token = set_ask_user_mode("auto")
                try:
                    result = await agent.run(content, session_id=session_id, run_id=run_id)
                    text = result.result if hasattr(result, "result") else str(result)
                finally:
                    if channel != "cli":
                        reset_ask_user_mode(token)

                router.respond(channel, user_id, text)
            except Exception as e:
                logger.exception(f"交互模式处理异常: {e}")
    finally:
        cli_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cli_task
