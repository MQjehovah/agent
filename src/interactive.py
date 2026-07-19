import asyncio
import contextlib
import logging
import os
import signal
from itertools import count

from agent.factory import AgentFactory
from channels import MessageRouter
from commands.handler import CommandHandler
from tui import TUIApp

logger = logging.getLogger("agent.main")


async def _wait_cancel(tui):
    while True:
        await asyncio.sleep(0.3)
        if tui.cancel_flag.is_set():
            return


async def interactive_mode():
    tui = None
    try:
        router = MessageRouter.instance()
        agent = await AgentFactory.instance().get_or_create(router.agent_name)
        session_id = router.format_session_id("cli", "1")

        ws_context = os.path.basename(os.path.normpath(agent.workspace))

        tui = TUIApp(agent)
        tui.setup(ws_context, branch="", session_id=session_id)
        tui.register_hooks(agent)

        ask_tool = agent.tool_registry.get_tool("ask_user") if agent.tool_registry else None
        tui.setup_ask_handler(ask_tool)

        cmd_handler = CommandHandler(agent, session_id, on_exit=tui.shutdown,
                                      output=tui.chat.append_output)

        await tui.start()

        def handle_signal():
            tui.shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                asyncio.get_running_loop().add_signal_handler(sig, handle_signal)

        task_counter = count(1)
        while not tui.is_shutdown:
            question = await tui.get_input()
            if question is None:
                break
            if not question.strip():
                continue
            if cmd_handler.is_command(question):
                await cmd_handler.handle(question)
                continue

            cmd_handler.set_current_task_id(next(task_counter))
            tui.start_task()
            tui.start_spinner()

            runner = asyncio.create_task(
                router.route(question, channel="cli", user_id="1"))
            watcher = asyncio.create_task(_wait_cancel(tui))
            done, _ = await asyncio.wait([runner, watcher], return_when=asyncio.FIRST_COMPLETED)
            watcher.cancel()

            if watcher in done:
                runner.cancel()
                await asyncio.wait([runner])

            await tui.stop_spinner()

            if runner.cancelled():
                logger.warning("任务被用户取消")
                tui.cancel_notice()
            elif exc := runner.exception():
                logger.exception(f"任务执行异常: {exc}")
                tui.error_notice(str(exc))
            else:
                result = runner.result()
                text = result.result if hasattr(result, "result") else str(result)
                tui.after_task(text)

            cmd_handler.set_current_task_id(None)

    except asyncio.CancelledError:
        logger.info("任务取消")
    except Exception as e:
        logger.exception(f"交互模式异常: {e}")
    finally:
        if tui:
            tui.shutdown()
