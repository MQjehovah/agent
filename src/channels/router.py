from typing import Any

from tools.ask_user import reset_ask_user_mode, set_ask_user_mode


def _default_role(user_id: str) -> str:
    """根据 user_id 解析角色，可按需扩展"""
    return "admin" if user_id in ("1", "cli:1", "admin") else "default"


class MessageRouter:
    """统一消息路由：所有渠道通过此路由器调用 agent.run()

    职责：
    - session_id = {channel}:{user_id}
    - 创建/复用 session 并注入用户身份
    - 非交互渠道自动设 ask_user_mode=auto
    """

    _instance: "MessageRouter | None" = None

    def __init__(self, agent_name: str = ""):
        self.agent_name = agent_name

    @classmethod
    def instance(cls) -> "MessageRouter":
        assert cls._instance is not None, "MessageRouter 尚未初始化"
        return cls._instance

    @staticmethod
    def format_session_id(channel: str, user_id: str) -> str:
        return f"{channel}:{user_id}"

    async def route(
        self,
        content: str,
        channel: str = "cli",
        user_id: str = "1",
        **kwargs,
    ) -> Any:
        from agent.factory import AgentFactory
        agent = await AgentFactory.instance().get_or_create(self.agent_name)

        session_id = self.format_session_id(channel, user_id)

        await agent.session_manager.create_session(
            session_id, user_id=user_id, role=_default_role(user_id),
            system_prompt=agent.system_prompt or "",
            agent_id=agent.agent_id,
        )

        if channel != "cli":
            token = set_ask_user_mode("auto")
        try:
            return await agent.run(task=content, session_id=session_id, **kwargs)
        finally:
            if channel != "cli":
                reset_ask_user_mode(token)
