"""P2-10：同轮工具并发上限（steering/队列化），避免瞬时打爆下游。"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import agent.loop as loopmod  # noqa: E402
from agent.loop import execute_tool_calls_parallel  # noqa: E402


class _FakeSession:
    def __init__(self):
        self.messages = []

    def add_message(self, role, content, **kwargs):
        msg = {"role": role, "content": content}
        msg.update(kwargs)
        self.messages.append(msg)


def _tc(tid, name):
    return {"id": tid, "type": "function", "function": {"name": name, "arguments": "{}"}}


class _Agent:
    name = "t"


async def _run(monkeypatch, limit, tool_count):
    monkeypatch.setenv("AGENT_TOOL_MAX_PARALLEL", str(limit))
    state = {"cur": 0, "max": 0}

    async def fake_exec(agent, name, args):
        state["cur"] += 1
        state["max"] = max(state["max"], state["cur"])
        await asyncio.sleep(0.05)
        state["cur"] -= 1
        return f"{name}-ok"

    monkeypatch.setattr(loopmod, "execute_tool_safe", fake_exec)
    tcs = [_tc(f"c{i}", f"t{i}") for i in range(tool_count)]
    session = _FakeSession()
    await execute_tool_calls_parallel(_Agent(), tcs, session)
    return state, session


@pytest.mark.asyncio
async def test_parallel_bounded_by_limit(monkeypatch):
    state, session = await _run(monkeypatch, 2, 6)
    assert state["max"] <= 2
    assert len([m for m in session.messages if m["role"] == "tool"]) == 6


@pytest.mark.asyncio
async def test_parallel_limit_zero_means_unlimited(monkeypatch):
    state, session = await _run(monkeypatch, 0, 4)
    assert state["max"] == 4
    assert len([m for m in session.messages if m["role"] == "tool"]) == 4
