import asyncio
from collections.abc import Callable
from typing import Any


class MessageRouter:
    """消息路由：输入输出解耦

    - publish(content, channel, user_id)      通道投递输入，立即返回
    - poll() → (content, channel, user_id)    interactive 消费
    - respond(channel, user_id, result)       interactive 写回结果
    - on_response(channel, user_id, cb)       通道注册结果回调
    """

    _instance: "MessageRouter | None" = None

    def __init__(self, agent_name: str = ""):
        self.agent_name = agent_name
        self._input_queue: asyncio.Queue = asyncio.Queue()
        self._listeners: dict[tuple[str, str], Callable] = {}

    @classmethod
    def instance(cls) -> "MessageRouter":
        assert cls._instance is not None, "MessageRouter 尚未初始化"
        return cls._instance

    @staticmethod
    def format_session_id(channel: str, user_id: str) -> str:
        return f"{channel}:{user_id}"

    def on_response(self, channel: str, user_id: str, callback: Callable):
        """通道注册结果回调"""
        self._listeners[(channel, user_id)] = callback

    def publish(self, content: str, channel: str = "cli", user_id: str = "1",
                run_id: str = ""):
        """通道投递输入，立即返回"""
        self._input_queue.put_nowait((content, channel, user_id, run_id))

    async def poll(self) -> tuple:
        """interactive 消费：取下一条输入 (content, channel, user_id, run_id)"""
        return await self._input_queue.get()

    def respond(self, channel: str, user_id: str, result: Any):
        """interactive 写回结果到通道"""
        cb = self._listeners.get((channel, user_id))
        if cb:
            cb(result)
