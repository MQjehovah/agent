import logging
import os

from agent.factory import AgentFactory
from channels import MessageRouter
from tui import TUIApp

logger = logging.getLogger("agent.main")


async def run():
    """CLI 通道：TUI 显示 + 生产者循环（consumer 由 interactive_mode 管理）"""
    agent = await AgentFactory.instance().get_or_create(
        MessageRouter.instance().agent_name)
    session_id = MessageRouter.format_session_id("cli", "1")

    tui = TUIApp(agent, context=os.path.basename(os.path.normpath(agent.workspace)),
                  session_id=session_id)
    await tui.start()

    MessageRouter.instance().on_response("cli", "1", tui.after_task)

    try:
        while not tui.is_shutdown:
            question = await tui.get_input()
            if question is None:
                break
            if not question.strip():
                continue
            tui.start_task()
            MessageRouter.instance().publish(
                question, channel="cli", user_id="1")
    finally:
        tui.shutdown()
