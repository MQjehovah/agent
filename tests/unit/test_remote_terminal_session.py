"""remote_terminal 连接优化：提示符完成、会话清理、busy 重试相关单测。"""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcp_server" / "src"))

import remote_terminal as rt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_sessions():
    """每个用例前后清空全局会话状态。"""
    rt.sessions.clear()
    for t in list(rt._idle_tasks.values()):
        t.cancel()
    rt._idle_tasks.clear()
    rt._session_locks.clear()
    yield
    for t in list(rt._idle_tasks.values()):
        t.cancel()
    rt._idle_tasks.clear()
    rt.sessions.clear()
    rt._session_locks.clear()


def test_text_ends_with_prompt_shell():
    parser = rt.TerminalParser()
    assert rt._text_ends_with_prompt(parser, "hello\nxzrobot@host:~$ ")
    assert not rt._text_ends_with_prompt(parser, "Password: ")
    assert not rt._text_ends_with_prompt(parser, "login: ")
    assert not rt._text_ends_with_prompt(parser, "partial output without prompt")


def test_ws_is_open_state_name():
    open_ws = MagicMock()
    open_ws.state = MagicMock(name="OPEN")
    open_ws.state.name = "OPEN"
    assert rt._ws_is_open(open_ws)

    closed = MagicMock()
    closed.state = MagicMock()
    closed.state.name = "CLOSED"
    assert not rt._ws_is_open(closed)
    assert not rt._ws_is_open(None)


def test_session_alive_requires_open_ws():
    sess = rt.TerminalSession(sn="SN1", is_connected=True, is_logged_in=True)
    closed = MagicMock()
    closed.state = MagicMock()
    closed.state.name = "CLOSED"
    sess.ws = closed
    assert not rt._session_alive(sess)

    opened = MagicMock()
    opened.state = MagicMock()
    opened.state.name = "OPEN"
    sess.ws = opened
    assert rt._session_alive(sess)


@pytest.mark.asyncio
async def test_receive_until_stops_on_prompt():
    sn = "SN_PROMPT"
    ws = AsyncMock()
    ws.state = MagicMock()
    ws.state.name = "OPEN"
    ws.recv = AsyncMock(
        side_effect=[
            b"line1\n",
            b"xzrobot@dev:~$ ",
            b"should_not_read",
        ]
    )
    sess = rt.TerminalSession(sn=sn, ws=ws, is_connected=True, is_logged_in=True)
    rt.sessions[sn] = sess

    outputs = await rt._receive_until(
        sn,
        overall_timeout=5.0,
        poll_timeout=0.2,
        stop_when=lambda t: rt._text_ends_with_prompt(sess.parser, t),
    )
    texts = "".join(o["data"] for o in outputs if o["type"] == "output")
    assert "line1" in texts
    assert "$" in texts
    assert ws.recv.await_count == 2


@pytest.mark.asyncio
async def test_run_command_returns_on_prompt_not_full_timeout():
    sn = "SN_CMD"
    ws = AsyncMock()
    ws.send = AsyncMock()
    ws.state = MagicMock()
    ws.state.name = "OPEN"
    ws.recv = AsyncMock(
        side_effect=[
            b"pwd\n",
            b"/opt/xzrobot\n",
            b"xzrobot@host:~$ ",
        ]
    )
    sess = rt.TerminalSession(sn=sn, ws=ws, is_connected=True, is_logged_in=True)
    rt.sessions[sn] = sess

    t0 = time.monotonic()
    raw, result = await rt._run_command(sn, "pwd", overall_timeout=30.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0
    assert "/opt/xzrobot" in result.output
    assert "xzrobot@host" not in result.output  # prompt 已剥除


@pytest.mark.asyncio
async def test_purge_session_removes_and_closes():
    sn = "SN_PURGE"
    ws = AsyncMock()
    ws.close = AsyncMock()
    ws.state = MagicMock()
    ws.state.name = "OPEN"
    sess = rt.TerminalSession(sn=sn, ws=ws, is_connected=True, is_logged_in=True)
    rt.sessions[sn] = sess
    rt._schedule_idle_disconnect(sn)
    assert sn in rt._idle_tasks

    await rt._purge_session(sn)
    assert sn not in rt.sessions
    assert sn not in rt._idle_tasks
    ws.close.assert_awaited()


@pytest.mark.asyncio
async def test_connect_busy_retries_then_succeeds():
    sn = "SN_BUSY"
    call_count = {"n": 0}

    async def fake_once(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise rt.SessionBusyError("会话已满")
        sess = rt.TerminalSession(sn=sn, is_connected=True, is_logged_in=True)
        ws = MagicMock()
        ws.state = MagicMock()
        ws.state.name = "OPEN"
        sess.ws = ws
        rt.sessions[sn] = sess
        return sess

    with patch.object(rt, "_connect_ws_once", side_effect=fake_once):
        with patch.object(rt, "BUSY_RETRY_BASE", 0.01):
            with patch.object(rt, "BUSY_RETRY_COUNT", 3):
                sess = await rt._connect_ws(sn, username="u", password="p")
    assert call_count["n"] == 3
    assert sess.sn == sn


@pytest.mark.asyncio
async def test_connect_reuses_alive_session():
    sn = "SN_REUSE"
    ws = MagicMock()
    ws.state = MagicMock()
    ws.state.name = "OPEN"
    sess = rt.TerminalSession(sn=sn, ws=ws, is_connected=True, is_logged_in=True)
    rt.sessions[sn] = sess

    with patch.object(rt, "_connect_ws_once", new_callable=AsyncMock) as once:
        got = await rt._connect_ws(sn)
        once.assert_not_called()
    assert got is sess


@pytest.mark.asyncio
async def test_connect_purges_stale_then_reconnects():
    sn = "SN_STALE"
    stale_ws = MagicMock()
    stale_ws.state = MagicMock()
    stale_ws.state.name = "CLOSED"
    stale = rt.TerminalSession(sn=sn, ws=stale_ws, is_connected=True, is_logged_in=True)
    rt.sessions[sn] = stale

    fresh = rt.TerminalSession(sn=sn, is_connected=True, is_logged_in=True)
    fresh_ws = MagicMock()
    fresh_ws.state = MagicMock()
    fresh_ws.state.name = "OPEN"
    fresh.ws = fresh_ws

    async def fake_once(*args, **kwargs):
        rt.sessions[sn] = fresh
        return fresh

    with patch.object(rt, "_connect_ws_once", side_effect=fake_once):
        got = await rt._connect_ws(sn, username="u", password="p")
    assert got is fresh
    assert rt._session_alive(got)


@pytest.mark.asyncio
async def test_ensure_session_auto_reconnect():
    sn = "SN_RECONN"
    dead = rt.TerminalSession(
        sn=sn,
        ws=None,
        is_connected=False,
        is_logged_in=False,
        username="xzrobot",
        password="secret",
    )
    rt.sessions[sn] = dead

    new_sess = rt.TerminalSession(sn=sn, is_connected=True, is_logged_in=True)
    ws = MagicMock()
    ws.state = MagicMock()
    ws.state.name = "OPEN"
    new_sess.ws = ws

    with patch.object(rt, "_connect_ws", new_callable=AsyncMock, return_value=new_sess) as conn:
        got = await rt._ensure_session(sn, auto_reconnect=True)
        conn.assert_awaited()
    assert got is new_sess


@pytest.mark.asyncio
async def test_idle_disconnect_purges(monkeypatch):
    sn = "SN_IDLE"
    monkeypatch.setattr(rt, "IDLE_TTL_SECONDS", 0.05)
    ws = AsyncMock()
    ws.close = AsyncMock()
    ws.state = MagicMock()
    ws.state.name = "OPEN"

    sess = rt.TerminalSession(sn=sn, ws=ws, is_connected=True, is_logged_in=True)
    sess.last_activity = time.time() - 1.0
    rt.sessions[sn] = sess
    rt._schedule_idle_disconnect(sn)

    await asyncio.sleep(0.15)
    assert sn not in rt.sessions
