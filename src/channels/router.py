import uuid
from typing import Any

from tools.ask_user import reset_ask_user_mode, set_ask_user_mode


class MessageRouter:
    """统一消息路由：所有渠道通过此路由器调用 agent.run()

    职责：
    - 标准化 session_id 格式 ({channel}:{unique_id})
    - 非交互渠道自动设 ask_user_mode=auto
    - 一致的用户身份传递
    - 可选的 hook 注册/注销（在 agent.run 前后自动处理）
    """

    def __init__(self, agent):
        self.agent = agent

    @staticmethod
    def format_session_id(channel: str, *parts: str) -> str:
        parts = [p for p in parts if p]
        return f"{channel}:" + ":".join(parts)

    async def route(
        self,
        content: str,
        channel: str = "cli",
        session_id: str = "",
        user_id: str = "",
        user_name: str = "",
        **kwargs,
    ) -> Any:
        if not session_id:
            session_id = self.format_session_id(channel, uuid.uuid4().hex[:8])

        if not user_id:
            user_id = f"{channel}:admin"
        if not user_name:
            user_name = "管理员" if channel == "cli" else channel

        if channel == "cli":
            return await self.agent.run(
                content, session_id=session_id,
                user_id=user_id, user_name=user_name,
                **kwargs,
            )
        else:
            token = set_ask_user_mode("auto")
            try:
                result = await self.agent.run(
                    content, session_id=session_id,
                    user_id=user_id, user_name=user_name,
                    **kwargs,
                )
                return result.result if hasattr(result, "result") else str(result)
            finally:
                reset_ask_user_mode(token)
