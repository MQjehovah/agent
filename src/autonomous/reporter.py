import logging

from autonomous.goal import Goal, PlanStep

logger = logging.getLogger("agent.autonomous.reporter")


class Reporter:
    async def report_progress(self, step: PlanStep, result) -> None:
        logger.info(
            "步骤进度: [%s] %s — 结果: %s",
            step.status,
            step.task_description,
            getattr(result, "result", result),
        )

    async def report_success(self, goal: Goal) -> None:
        logger.info("目标完成: %s", goal.title)

    async def report_failure(self, goal, verification) -> None:
        logger.warning(
            "目标失败: %s — %s",
            goal.title,
            verification.summary,
        )

    async def ask_confirmation(self, step: PlanStep) -> bool:
        logger.info("确认步骤: %s (自动确认)", step.task_description)
        return True


class DingTalkReporter(Reporter):
    def __init__(self, dingtalk_plugin=None, default_session_id: str = ""):
        self.dingtalk_plugin = dingtalk_plugin
        self.default_session_id = default_session_id

    async def report_progress(self, step: PlanStep, result) -> None:
        await super().report_progress(step, result)
        title = f"步骤更新: {step.status}"
        content = f"## {title}\n\n**任务:** {step.task_description}\n\n**结果:** {getattr(result, 'result', result)}"
        await self._send_markdown(title, content)

    async def report_success(self, goal: Goal) -> None:
        await super().report_success(goal)
        title = "目标完成"
        content = f"## {title}\n\n**{goal.title}**\n\n{goal.description}"
        await self._send_markdown(title, content)

    async def report_failure(self, goal, verification) -> None:
        await super().report_failure(goal, verification)
        title = "目标失败"
        content = (
            f"## {title}\n\n**{goal.title}**\n\n"
            f"**摘要:** {verification.summary}\n\n"
            f"**反馈:** {verification.feedback}"
        )
        await self._send_markdown(title, content)

    async def _send_markdown(self, title: str, content: str) -> None:
        if not self.dingtalk_plugin:
            logger.debug("钉钉插件未配置，跳过发送")
            return
        try:
            session_id = self.default_session_id
            sessions = getattr(self.dingtalk_plugin, "sessions", {})
            if session_id and session_id in sessions:
                session = sessions[session_id]
                conversation_id = getattr(session, "conversation_id", "")
                if conversation_id:
                    send_coro = self.dingtalk_plugin.send_markdown_message(
                        conversation_id, title, content
                    )
                    if hasattr(send_coro, "__await__"):
                        await send_coro
                    logger.debug("钉钉消息已发送: %s", title)
            else:
                logger.debug("钉钉会话未找到: %s", session_id)
        except Exception as e:
            logger.warning("钉钉消息发送失败: %s", e)

    async def ask_confirmation(self, step: PlanStep) -> bool:
        title = "请求确认"
        content = f"## {title}\n\n**任务:** {step.task_description}\n\n> 自动确认（未来支持人工回复）"
        await self._send_markdown(title, content)
        return True
