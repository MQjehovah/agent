import asyncio
import contextvars
import inspect
import logging
from collections import defaultdict
from typing import Callable

from .types import HookEvent, HookContext

logger = logging.getLogger("agent.hooks")


# 当前 run 的流式事件作用域标识。
# - 仅由“顶层” run（直接响应一个外部请求的 run）通过 set_run_id() 建立；
# - 嵌套的子代理 run 不重置它，因此整条调用树（主代理 + 子代理）共享同一个 run_id；
# - 这样并发用户各自的 run 拥有不同 run_id，HookManager.fire 只把事件派发给
#   注册了匹配 run_id 的回调（或 run_id 为 None 的全局回调），杜绝跨流串话。
_hook_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "hook_run_id", default=""
)


def set_run_id(run_id: str):
    """建立当前 run 的流式作用域，返回 reset token（仅顶层 run 应调用）。"""
    return _hook_run_id.set(run_id)


def get_run_id() -> str:
    return _hook_run_id.get()


def reset_run_id(token) -> None:
    _hook_run_id.reset(token)


class HookManager:
    def __init__(self):
        # event -> [(callback, run_id)]; run_id 为 None 表示全局回调（始终触发）
        self._hooks: dict[HookEvent, list[tuple[Callable, str | None]]] = defaultdict(list)

    def register(self, event: HookEvent, callback: Callable, run_id: str | None = None):
        """注册回调。run_id 非 None 时，仅在 fire 的 run_id 与之匹配时触发。"""
        self._hooks[event].append((callback, run_id))

    async def fire(self, event: HookEvent, **kwargs):
        """触发事件，按顺序执行匹配的回调。

        回调匹配规则：注册时 run_id 为 None（全局回调）始终执行；
        否则仅当注册 run_id 等于当前 run 作用域 run_id 时执行。
        """
        current_rid = get_run_id()
        context = HookContext(event=event, run_id=current_rid, **kwargs)
        for callback, cb_run_id in self._hooks.get(event, []):
            # 全局回调（cb_run_id is None）或 run_id 匹配的回调才触发
            if cb_run_id is not None and cb_run_id != current_rid:
                continue
            try:
                result = callback(context)
                # 支持同步和异步回调
                if inspect.iscoroutine(result):
                    await result
            except Exception as e:
                event_name = event.value if hasattr(event, 'value') else str(event)
                logger.error(f"钩子回调执行失败 [{event_name}]: {e}")

    def unregister(self, event: HookEvent, callback: Callable):
        if event in self._hooks:
            self._hooks[event] = [
                (cb, rid) for (cb, rid) in self._hooks[event] if cb is not callback
            ]

    def clear(self, event: HookEvent = None):
        if event:
            self._hooks.pop(event, None)
        else:
            self._hooks.clear()
