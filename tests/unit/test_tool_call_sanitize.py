"""悬空 tool_calls 修复回归：请求侧清洗 + 工具执行取消时补全落库。

线上事故: 在线会话新一轮 400 —— assistant 带 N 个 tool_calls 但历史里 tool 结果
缺条(工具执行中途被取消/worker 回收)。本文件锁定:
- sanitize_tool_message_pairs: 悬空补合成(按 tool_calls 顺序)/孤儿丢弃/健康原样;
- think/think_stream 请求边界: 清洗并对齐 session.messages;
- execute_tool_calls_parallel: 顺序+并行分支取消时 finally 补全, 异常继续抛出;
- _restore_db_session_history: 从 DB 恢复悬空历史后上下文可直接送 LLM。
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest  # noqa: E402

from agent.loop import _sanitize_llm_messages, execute_tool_calls_parallel  # noqa: E402
from agent.session import TOOL_RESULT_LOST, sanitize_tool_message_pairs  # noqa: E402

INTERRUPTED = "（执行被中断，无结果）"

EXEC_LOGGER = "agent.agent"


def _assistant(tc_ids, content=""):
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {"id": tid, "type": "function", "function": {"name": f"tool_{tid}", "arguments": "{}"}}
            for tid in tc_ids
        ],
    }


def _tool(tid, content="ok"):
    return {"role": "tool", "tool_call_id": tid, "content": content}


class _FakeSession:
    def __init__(self, messages=None, session_id="web:u1:cancel"):
        self.messages = list(messages or [])
        self.session_id = session_id

    def add_message(self, role, content, **kwargs):
        msg = {"role": role, "content": content}
        msg.update(kwargs)
        self.messages.append(msg)


# ---------------- sanitize_tool_message_pairs ----------------

def test_sanitize_empty_messages_returns_empty():
    messages, fixed = sanitize_tool_message_pairs([])
    assert messages == []
    assert fixed == 0


def test_sanitize_healthy_pairs_unchanged():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "查天气"},
        _assistant(["c1"]),
        _tool("c1", "晴 25℃"),
        _assistant([], content="好的"),
    ]
    out, fixed = sanitize_tool_message_pairs(msgs)
    assert fixed == 0
    assert out is msgs


def test_sanitize_dangling_single_backfills_result():
    msgs = [{"role": "user", "content": "查天气"}, _assistant(["call_1"])]
    out, fixed = sanitize_tool_message_pairs(msgs)
    assert fixed == 1
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert tool_msgs == [{"role": "tool", "tool_call_id": "call_1", "content": TOOL_RESULT_LOST}]
    assert out[-1] is tool_msgs[0]  # 合成结果紧跟 assistant 之后
    assert out[0] is msgs[0]


def test_sanitize_dangling_multiple_backfills_in_tool_calls_order():
    msgs = [
        {"role": "user", "content": "u"},
        _assistant(["c1", "c2", "c3"]),
        _tool("c3", "第三"),
    ]
    out, fixed = sanitize_tool_message_pairs(msgs)
    assert fixed == 2
    tool_ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    # 已有结果原位保留；缺失项按 tool_calls 顺序补在组末尾
    assert tool_ids == ["c3", "c1", "c2"]
    assert [m["content"] for m in out if m.get("role") == "tool"] == ["第三", TOOL_RESULT_LOST, TOOL_RESULT_LOST]


def test_sanitize_orphan_tool_dropped():
    msgs = [{"role": "user", "content": "u"}, _tool("ghost"), _assistant(["c1"]), _tool("c1")]
    out, fixed = sanitize_tool_message_pairs(msgs)
    assert fixed == 1
    assert out == [msgs[0], msgs[2], msgs[3]]
    assert all(m.get("tool_call_id") != "ghost" for m in out)


def test_sanitize_mixed_groups_counts_synth_and_orphans():
    msgs = [
        {"role": "system", "content": "sys"},
        _assistant(["a1", "a2"]),
        _tool("a1"),
        {"role": "user", "content": "继续"},
        _assistant(["b1"]),
        _tool("a2"),      # 位置错乱：属于上一组，出现在 B 组 → 丢弃
        _tool("ghost"),   # 无任何 assistant 声明 → 孤儿丢弃
    ]
    out, fixed = sanitize_tool_message_pairs(msgs)
    # A 组缺 a2 补 1；B 组缺 b1 补 1；错位/孤儿各丢 1
    assert fixed == 4
    assert [m["tool_call_id"] for m in out if m.get("role") == "tool"] == ["a1", "a2", "b1"]
    assert [m["content"] for m in out if m.get("role") == "tool"] == ["ok", TOOL_RESULT_LOST, TOOL_RESULT_LOST]


def test_sanitize_llm_boundary_writes_back_session():
    """请求侧清洗：messages 是 session.messages 本体时同步回写。"""
    sess = _FakeSession([_assistant(["x"])])
    request_messages = _sanitize_llm_messages(sess.messages, sess)
    assert request_messages is sess.messages
    assert [m["tool_call_id"] for m in sess.messages if m.get("role") == "tool"] == ["x"]


def test_sanitize_llm_boundary_copy_keeps_session_dirty():
    """retry 副本清洗只影响本次请求，不把副本(含注入消息)写回会话。"""
    sess = _FakeSession([_assistant(["x"])])
    request_copy = list(sess.messages) + [{"role": "user", "content": "重试提示"}]
    request_messages = _sanitize_llm_messages(request_copy, sess)
    assert request_messages is not request_copy
    assert "x" in [m.get("tool_call_id") for m in request_messages if m.get("role") == "tool"]
    assert all(m.get("role") != "tool" for m in sess.messages)  # 会话本体未被替换


def test_sanitize_restore_db_history_closes_dangling(tmp_path):
    """恢复路径：DB 中含悬空 tool_calls → 恢复后上下文无悬空，可安全送 LLM。"""
    from agent.core import Agent
    from agent.session import AgentSession
    from storage.storage import Storage

    ws = tmp_path / "ws"
    ws.mkdir()
    st = Storage(str(ws))
    try:
        sid = "web:u1:restore1"
        st.save_message_sync("agent1", sid, "user", "帮我查天气", user_id="u1")
        st.save_message_sync("agent1", sid, "assistant", "",
                             tool_calls=_assistant(["c1", "c2"])["tool_calls"])
        st.save_message_sync("agent1", sid, "tool", "晴 25℃",
                             tool_call_id="c1", name="weather")
        # c2 缺失：模拟工具执行中途被取消

        fresh = AgentSession(session_id=sid, system_prompt="你是助手")
        n = Agent._restore_db_session_history(SimpleNamespace(storage=st), fresh)
        assert n == 3

        msgs = fresh.messages
        tc_ids = {tc["id"] for m in msgs
                  if m.get("role") == "assistant" and m.get("tool_calls") for tc in m["tool_calls"]}
        tool_ids = [m.get("tool_call_id") for m in msgs if m.get("role") == "tool"]
        assert sorted(tool_ids) == sorted(tc_ids) == ["c1", "c2"]
        by_id = {m["tool_call_id"]: m for m in msgs if m.get("role") == "tool"}
        assert by_id["c1"]["content"] == "晴 25℃"
        assert by_id["c2"]["content"] == TOOL_RESULT_LOST
        # 已闭合：再清洗无修复
        _, fixed = sanitize_tool_message_pairs(msgs)
        assert fixed == 0
    finally:
        st.close()


# ---------------- execute_tool_calls_parallel 取消补全 ----------------

def _make_exec_agent(tool_runner):
    """最小 agent 替身：走 execute_tool_safe → execute_tool → tool_registry 链路。"""
    agent = MagicMock()
    agent.permission.check.return_value = SimpleNamespace(reason="")
    agent.rbac = None
    agent.on_confirm = None
    agent.sandbox = None
    agent.plugin_manager = None
    agent._circuit_breaker = None
    agent.hooks.fire = AsyncMock()
    agent.tracer = MagicMock()
    agent.tool_registry.has_tool.return_value = True
    agent.tool_registry.execute = AsyncMock(side_effect=tool_runner)
    agent.mcp = None
    agent.skill_manager = None
    agent.subagent_manager = None
    return agent


def _tc(tid, name):
    return {"id": tid, "type": "function", "function": {"name": name, "arguments": "{}"}}


async def test_tool_call_normal_completion_writes_exactly_one_each():
    async def _execute(name, args):
        return f"{name}-ok"

    agent = _make_exec_agent(_execute)
    tcs = [_tc("call_fast", "fast_tool"), _tc("call_slow", "slow_tool")]
    session = _FakeSession()
    await execute_tool_calls_parallel(agent, tcs, session)
    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_fast", "call_slow"]
    assert [m["content"] for m in tool_msgs] == ["fast_tool-ok", "slow_tool-ok"]


async def test_tool_call_cancelled_sequential_backfills_result():
    """顺序分支（<=1 个工具）：执行中被取消 → 合成结果落库，异常继续抛出。"""
    started = asyncio.Event()

    async def _hang(name, args):
        started.set()
        await asyncio.Event().wait()

    agent = _make_exec_agent(_hang)
    tc = _tc("call_slow", "slow_tool")
    session = _FakeSession([{"role": "assistant", "content": "", "tool_calls": [tc]}])
    task = asyncio.create_task(execute_tool_calls_parallel(agent, [tc], session))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_slow"]
    assert tool_msgs[0]["content"] == INTERRUPTED
    assert tool_msgs[0]["name"] == "slow_tool"


async def test_tool_call_cancelled_parallel_backfills_all_results():
    """并行分支：首个工具完成后取消 → 本轮所有 tool_calls 都有结果，异常继续抛出。"""
    fast_done = asyncio.Event()

    async def _execute(name, args):
        if name == "fast_tool":
            fast_done.set()
            return "fast-ok"
        await asyncio.Event().wait()

    agent = _make_exec_agent(_execute)
    fast = _tc("call_fast", "fast_tool")
    slow = _tc("call_slow", "slow_tool")
    session = _FakeSession([{"role": "assistant", "content": "", "tool_calls": [fast, slow]}])
    task = asyncio.create_task(execute_tool_calls_parallel(agent, [fast, slow], session))
    await asyncio.wait_for(fast_done.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    tool_msgs = [m for m in session.messages if m.get("role") == "tool"]
    assert sorted(m["tool_call_id"] for m in tool_msgs) == ["call_fast", "call_slow"]
    assert all(m["content"] == INTERRUPTED for m in tool_msgs)
    # 每个 tool_call 均有对应 tool 消息 → 送 LLM 不再悬空
    _, fixed = sanitize_tool_message_pairs(session.messages)
    assert fixed == 0


async def test_tool_call_backfill_failure_only_warns(caplog):
    """补写失败只 warning：取消异常照常抛出，不被落库错误掩盖。"""
    started = asyncio.Event()

    class _BadSession(_FakeSession):
        def add_message(self, *args, **kwargs):
            raise RuntimeError("storage down")

    async def _hang(name, args):
        started.set()
        await asyncio.Event().wait()

    agent = _make_exec_agent(_hang)
    tc = _tc("call_1", "slow_tool")
    session = _BadSession()
    task = asyncio.create_task(execute_tool_calls_parallel(agent, [tc], session))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with caplog.at_level(logging.WARNING, logger=EXEC_LOGGER), pytest.raises(asyncio.CancelledError):
        await task
    assert any("工具结果落库失败" in rec.getMessage() for rec in caplog.records)
