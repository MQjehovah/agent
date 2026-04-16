import asyncio
import inspect
import logging
from collections import defaultdict
from typing import Callable

from .types import HookEvent, HookContext

logger = logging.getLogger("agent.hooks")


class HookManager:
    def __init__(self):
        self._hooks: dict[HookEvent, list[Callable]] = defaultdict(list)

    def register(self, event: HookEvent, callback: Callable):
        self._hooks[event].append(callback)

    async def fire(self, event: HookEvent, **kwargs):
        """触发事件，按顺序执行所有回调"""
        context = HookContext(event=event, **kwargs)
        for callback in self._hooks.get(event, []):
            try:
                result = callback(context)
                # 支持同步和异步回调
                if inspect.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"钩子回调执行失败 [{event.value}]: {e}")

    def unregister(self, event: HookEvent, callback: Callable):
        if event in self._hooks and callback in self._hooks[event]:
            self._hooks[event].remove(callback)

    def clear(self, event: HookEvent = None):
        if event:
            self._hooks.pop(event, None)
        else:
            self._hooks.clear()
