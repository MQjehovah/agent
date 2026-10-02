"""P0-2：内部 typed 事件流 AgentEvent / EventEmitter 及与 HookManager 的兼容。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.events import AgentEvent, AgentEventType, EventEmitter, emit_agent_event  # noqa: E402
from hooks import HookEvent, HookManager  # noqa: E402


@pytest.mark.asyncio
async def test_emit_buffers_and_notifies_listeners():
    em = EventEmitter()
    seen = []
    em.subscribe(lambda ev: seen.append(ev))

    ev = await em.emit(AgentEventType.TOOL_START, tool_name="edit")
    assert isinstance(ev, AgentEvent)
    assert ev.type is AgentEventType.TOOL_START
    assert ev.data["tool_name"] == "edit"
    assert seen and seen[0].type is AgentEventType.TOOL_START


@pytest.mark.asyncio
async def test_async_listener_supported():
    em = EventEmitter()
    seen = []

    async def listener(ev):
        seen.append(ev.type)

    em.subscribe(listener)
    await em.emit(AgentEventType.RUN_END)
    assert seen == [AgentEventType.RUN_END]


@pytest.mark.asyncio
async def test_listener_exception_does_not_break():
    em = EventEmitter()

    def bad(_ev):
        raise RuntimeError("boom")

    em.subscribe(bad)
    ev = await em.emit(AgentEventType.ERROR, message="x")
    assert ev.type is AgentEventType.ERROR


@pytest.mark.asyncio
async def test_compat_fires_hook_manager():
    hooks = HookManager()
    seen = []

    async def on_round(ctx):
        seen.append(ctx.metadata.get("iteration"))

    hooks.register(HookEvent.ROUND_START, on_round)
    em = EventEmitter(hooks=hooks)

    await em.emit(AgentEventType.ROUND_START, metadata={"iteration": 3})
    assert seen == [3]


@pytest.mark.asyncio
async def test_buffer_is_bounded():
    em = EventEmitter(max_buffer=3)
    for i in range(5):
        await em.emit(AgentEventType.TOOL_START, tool_name=str(i))
    buf = em.buffer
    assert len(buf) == 3
    assert buf[-1].data["tool_name"] == "4"


@pytest.mark.asyncio
async def test_emit_agent_event_falls_back_to_hooks():
    class _Agent:
        def __init__(self):
            self.hooks = HookManager()
            self._hook_event = HookEvent

    agent = _Agent()
    seen = []
    agent.hooks.register(HookEvent.TOOL_RESULT, lambda ctx: seen.append(ctx.tool_name))
    await emit_agent_event(agent, AgentEventType.TOOL_RESULT, tool_name="git")
    assert seen == ["git"]


@pytest.mark.asyncio
async def test_emit_agent_event_prefers_emitter():
    class _Agent:
        def __init__(self):
            self.events = EventEmitter()

    agent = _Agent()
    seen = []
    agent.events.subscribe(lambda ev: seen.append(ev.data.get("tool_name")))
    await emit_agent_event(agent, AgentEventType.TOOL_START, tool_name="edit")
    assert seen == ["edit"]
