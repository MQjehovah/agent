import uuid
from typing import Any

from channels.run_registry import register_run, unregister_run
from tools.ask_user import reset_ask_user_mode, set_ask_user_mode


class MessageRouter:
    """统一消息路由：所有渠道通过此路由器调用 agent.run()

    职责：
    - 标准化 session_id 格式 ({channel}:{unique_id})
    - 非交互渠道自动设 ask_user_mode=auto
    - 一致的用户身份传递
    - 运行中登记：除 cli（本机交互，不属线上会话）外，route 前后对会话做
      开始/结束登记，使「运行中」API 对钉钉/飞书/webhook/定时等渠道同样可见。
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
        group_context: bool = False,
        return_result: bool = False,
        **kwargs,
    ) -> Any:
        if not session_id:
            session_id = self.format_session_id(channel, uuid.uuid4().hex[:8])

        if not user_id:
            user_id = f"{channel}:admin"
        if not user_name:
            user_name = "管理员" if channel == "cli" else channel

        tracked = channel != "cli"
        if tracked:
            model = ""
            try:
                model = getattr(getattr(self.agent, "client", None), "model", "") or ""
            except Exception:
                model = ""
            register_run(session_id, channel, user_id, model=model)
        try:
            if channel == "cli":
                return await self.agent.run(
                    content, session_id=session_id,
                    user_id=user_id, user_name=user_name,
                    group_context=group_context,
                    **kwargs,
                )
            else:
                token = set_ask_user_mode("auto")
                try:
                    result = await self.agent.run(
                        content, session_id=session_id,
                        user_id=user_id, user_name=user_name,
                        group_context=group_context,
                        **kwargs,
                    )
                    # 渠道层需要结果元信息(如钉钉群敏感标记改道)时返回 AgentResult；
                    # 默认保持既有行为: 解包为最终回复文本字符串。
                    if return_result:
                        return result
                    return result.result if hasattr(result, "result") else str(result)
                finally:
                    reset_ask_user_mode(token)
        finally:
            if tracked:
                unregister_run(session_id)
