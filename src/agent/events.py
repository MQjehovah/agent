"""内部 typed 事件流 —— 内核产生 `AgentEvent`，hooks / SSE / 渠道 / 桌面作为消费者。

设计对齐 Pi：内核只负责产生结构化事件，不关心谁消费。既有 `HookManager` 降级为
"消费者之一"（通过 `_HOOK_MAP` 做 event → HookEvent 兼容转发），保证零回归。
"""
from __future__ import annotations

import contextlib
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hooks.manager import get_run_id
from hooks.types import HookEvent

logger = logging.getLogger("agent.events")


class AgentEventType(str, Enum):
    RUN_START = "run_start"
    THINK_START = "think_start"
    ROUND_START = "round_start"
    LLM_RESPONSE = "llm_response"
    CHAT_EVENT = "chat_event"
    TOOL_START = "tool_start"
    TOOL_RESULT = "tool_result"
    POST_TOOL_USE = "post_tool_use"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_RESULT = "subagent_result"
    ERROR = "error"
    RUN_END = "run_end"


# 事件 → 既有 HookEvent 的兼容映射（无映射的事件只进事件流/缓冲，不触发 HookManager）
_HOOK_MAP: dict[AgentEventType, HookEvent] = {
    AgentEventType.RUN_START: HookEvent.AGENT_START,
    AgentEventType.ROUND_START: HookEvent.ROUND_START,
    AgentEventType.LLM_RESPONSE: HookEvent.LLM_RESPONSE,
    AgentEventType.CHAT_EVENT: HookEvent.CHAT_EVENT,
    AgentEventType.TOOL_START: HookEvent.TOOL_START,
    AgentEventType.TOOL_RESULT: HookEvent.TOOL_RESULT,
    AgentEventType.POST_TOOL_USE: HookEvent.POST_TOOL_USE,
    AgentEventType.SUBAGENT_START: HookEvent.SUBAGENT_START,
    AgentEventType.SUBAGENT_RESULT: HookEvent.SUBAGENT_RESULT,
    AgentEventType.RUN_END: HookEvent.AGENT_STOP,
}


@dataclass
class AgentEvent:
    type: AgentEventType
    run_id: str = ""
    ts: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)


Listener = Callable[[AgentEvent], Any]


class EventEmitter:
    """内核事件发射器：环形缓冲 + 订阅 + 兼容 HookManager。

    - `subscribe(listener)` 注册消费者（同步/异步均可），返回退订函数；
    - `emit(type, **fields)` 生成 `AgentEvent`、通知监听器、再转发给 HookManager；
    - 任一消费者/兼容 Hook 失败只告警，绝不影响主流程。
    """

    def __init__(self, hooks=None, max_buffer: int = 200):
        self._listeners: list[Listener] = []
        self._buffer: list[AgentEvent] = []
        self._max_buffer = max(1, int(max_buffer))
        self.hooks = hooks

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return _unsubscribe

    @property
    def buffer(self) -> list[AgentEvent]:
        """最近事件的快照（供诊断/测试；容量受 max_buffer 限制）。"""
        return list(self._buffer)

    async def emit(self, event_type: AgentEventType, **fields) -> AgentEvent:
        event = AgentEvent(type=event_type, run_id=get_run_id(), data=dict(fields))
        self._buffer.append(event)
        if len(self._buffer) > self._max_buffer:
            self._buffer = self._buffer[-self._max_buffer:]

        for listener in list(self._listeners):
            try:
                out = listener(event)
                if inspect.iscoroutine(out):
                    await out
            except Exception as e:  # noqa: BLE001
                logger.error(f"事件监听器失败 [{event_type.value}]: {e}")

        hook_event = _HOOK_MAP.get(event_type)
        if hook_event is not None and self.hooks is not None:
            try:
                await self.hooks.fire(hook_event, **fields)
            except Exception as e:  # noqa: BLE001
                logger.error(f"事件兼容 Hook 触发失败 [{event_type.value}]: {e}")
        return event


async def emit_agent_event(agent, event_type: AgentEventType, **fields) -> None:
    """在 agent 上发射内核事件。

    优先走 `agent.events`（typed 流）；无 emitter 的精简/测试 agent 回退到
    `agent.hooks.fire`（行为与旧代码一致）。
    """
    emitter = getattr(agent, "events", None)
    if isinstance(emitter, EventEmitter):
        await emitter.emit(event_type, **fields)
        return
    hook_event = _HOOK_MAP.get(event_type)
    hooks = getattr(agent, "hooks", None)
    if hook_event is not None and hooks is not None:
        await hooks.fire(hook_event, **fields)
