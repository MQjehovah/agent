"""自研 MCP servers v2(MCPServer) 进程内测试。

v2 关键行为:
- `FastMCP` 改名为 `MCPServer`, `@mcp.tool()` / `mcp.run()` 用法不变;
- 同步 handler 跑在 anyio worker 线程而非事件循环线程, `remote_terminal` 的
  终端类工具必须改 `async def`(否则 `run_until_complete` 在 worker 线程直接报错,
  且 websockets/Queue 绑定主 loop 跨线程使用不安全);
- 读写全局 `sessions`/`WS_BASE_URL` 的工具同样改 async, 与事件循环串行;
- 纯函数工具(`strip_ansi`/`parse_output`)保持同步, 把解析开销留在 worker 线程。

测试用官方 v2 进程内写法 `Client(module.mcp, raise_exceptions=True)`, 不启子进程、
不触网、不触设备; `mysql_query` 只走纯校验分支, 显式禁止真连 DB。
"""
import importlib
import inspect
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_SRC = ROOT / "mcp_server" / "src"
if str(MCP_SRC) not in sys.path:
    sys.path.insert(0, str(MCP_SRC))

import env_guard  # noqa: E402
from mcp import Client  # noqa: E402

# 各 server 的工具数(v2 迁移后的 173 个工具需一个不少; remote_operation/ticket_ops
# 为生产侧独有模块, 已回流仓库并同步迁移; W3 新增 mcp_time/mcp_fetch/mcp_filesystem,
# W3b 新增 mcp_git/mcp_postgres)
SERVER_TOOL_COUNTS = {
    "default": 2,
    # 15(消息/通讯录/卡片) + 10(审批/待办/日程, 2026-09 办公 API 扩展)
    "dingtalk": 25,
    "mysql_query": 3,
    "remote_terminal": 15,
    "rosiwit_cloud_remote": 71,
    "rosiwit_cloud_ticket": 7,
    "comfyui_remote": 17,
    "remote_operation": 36,
    "ticket_ops": 7,
    "mcp_time": 3,
    "mcp_fetch": 2,
    "mcp_filesystem": 8,
    "mcp_git": 11,
    "mcp_postgres": 3,
}

REMOTE_TERMINAL_ASYNC_TOOLS = (
    "connect_terminal",
    "disconnect_terminal",
    "send_command",
    "send_raw",
    "receive_output",
    "resize_terminal",
    "get_session_status",
    "clear_buffer",
    "get_buffer",
    "interactive_session",
    "set_ws_base_url",
    "execute_with_retry",
    "wait_for_prompt",
)

REMOTE_TERMINAL_SYNC_TOOLS = ("strip_ansi", "parse_output")

SN = "SN-TEST"


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch):
    """注入假秘密, 规避 require_secret 的生产校验(无需真实凭证即可导入 server)。"""
    monkeypatch.setattr(
        env_guard, "require_secret", lambda name, value, *args, **kwargs: value or f"test-{name}"
    )


def _load(name):
    return importlib.import_module(name)


async def _list_tools(module):
    async with Client(module.mcp, raise_exceptions=True) as client:
        return await client.list_tools()


async def _call(module, name, arguments=None):
    async with Client(module.mcp, raise_exceptions=True) as client:
        return await client.call_tool(name, arguments or {})


def _payload(result):
    """把 CallToolResult 还原为 dict/str: 优先 structured_content, 否则解析文本 JSON。"""
    assert result.is_error is not True
    if result.structured_content is not None:
        return result.structured_content
    text = "\n".join(getattr(item, "text", "") or "" for item in (result.content or []))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@pytest.mark.parametrize("name", list(SERVER_TOOL_COUNTS))
async def test_server_lists_expected_tools(name):
    module = _load(name)
    result = await _list_tools(module)
    assert len(result.tools) == SERVER_TOOL_COUNTS[name]
    # v2 字段为 snake_case
    assert all(tool.input_schema is not None for tool in result.tools)


async def test_default_get_current_time():
    module = _load("default")
    payload = _payload(await _call(module, "get_current_time"))
    assert payload["datetime"]
    assert payload["iso"]
    assert payload["weekday"].startswith("星期")


async def test_dingtalk_reports_missing_config_without_network(monkeypatch):
    module = _load("dingtalk")
    monkeypatch.setattr(module, "APP_KEY", "")
    monkeypatch.setattr(module, "APP_SECRET", "")
    payload = _payload(await _call(module, "dingtalk_get_access_token"))
    assert isinstance(payload, str)
    assert "未配置" in payload


def test_dingtalk_access_token_fetched_once_under_concurrency(monkeypatch):
    module = _load("dingtalk")
    calls = []

    class FakeResponse:
        def json(self):
            return {"accessToken": "tok-1", "expireIn": 7200}

    def fake_post(*args, **kwargs):
        calls.append(1)
        time.sleep(0.05)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)
    monkeypatch.setattr(module, "_access_token_cache", {"token": "", "expire_at": 0})

    results = []
    threads = [threading.Thread(target=lambda: results.append(module._get_access_token())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == [1]
    assert results == ["tok-1"] * 8


async def test_mysql_query_rejects_non_select_without_db(monkeypatch):
    module = _load("mysql_query")

    def boom(**kwargs):
        raise AssertionError("测试禁止连接数据库")

    monkeypatch.setattr(module.pymysql, "connect", boom)
    payload = _payload(await _call(module, "execute_query", {"query": "DROP TABLE users"}))
    assert payload == {"error": "只允许SELECT查询"}


async def test_ticket_lists_statuses():
    module = _load("rosiwit_cloud_ticket")
    payload = _payload(await _call(module, "list_ticket_statuses"))
    assert payload["success"] is True
    assert len(payload["statuses"]) == 6


async def test_ticket_ops_lists_statuses():
    """生产侧独有的 ticket_ops 回流后同规则: list_ticket_statuses 纯映射, 不触网。"""
    module = _load("ticket_ops")
    payload = _payload(await _call(module, "list_ticket_statuses"))
    assert payload["success"] is True
    assert len(payload["statuses"]) == 6



async def test_cloud_remote_validates_blank_device_without_network():
    module = _load("rosiwit_cloud_remote")
    payload = _payload(await _call(module, "device_backward", {"device_id": "", "product_id": ""}))
    assert payload["success"] is False
    assert "device_id" in payload["error"]


async def test_comfyui_server_switch_roundtrip():
    module = _load("comfyui_remote")
    original = module.STATE["base_url"]
    try:
        payload = _payload(await _call(module, "set_comfyui_server", {"url": "http://127.0.0.1:9"}))
        assert payload == {"base_url": "http://127.0.0.1:9"}
        payload = _payload(await _call(module, "get_comfyui_server"))
        assert payload == {"base_url": "http://127.0.0.1:9"}
    finally:
        module.STATE["base_url"] = original
        module._object_info_cache["data"] = None


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


@pytest.fixture
def terminal_session(fake_secrets):
    """装一个假终端会话(假 ws + 空队列), 不真连 WebSocket。"""
    module = _load("remote_terminal")
    original_sessions = dict(module.sessions)
    original_base_url = module.WS_BASE_URL
    session = module.TerminalSession(sn=SN, ws=FakeWebSocket(), is_connected=True, is_logged_in=True)
    session.sid = "SID-TEST"
    module.sessions[SN] = session
    yield module, session
    module.sessions.clear()
    module.sessions.update(original_sessions)
    module.WS_BASE_URL = original_base_url


async def test_remote_terminal_handler_kinds(terminal_session):
    module, _ = terminal_session
    for name in REMOTE_TERMINAL_ASYNC_TOOLS:
        assert inspect.iscoroutinefunction(getattr(module, name)), f"{name} 必须为 async"
    for name in REMOTE_TERMINAL_SYNC_TOOLS:
        assert not inspect.iscoroutinefunction(getattr(module, name))


async def test_remote_terminal_connect_and_disconnect(terminal_session, monkeypatch):
    module, session = terminal_session
    fake_ws = session.ws

    async def fake_connect_ws(sn, cols=80, rows=24, username=None, password=None):
        return module.sessions[sn]

    monkeypatch.setattr(module, "_connect_ws", fake_connect_ws)

    payload = _payload(await _call(module, "connect_terminal", {"sn": SN}))
    assert payload["success"] is True
    assert payload["sid"] == "SID-TEST"

    payload = _payload(await _call(module, "disconnect_terminal", {"sn": SN}))
    assert payload["success"] is True
    assert SN not in module.sessions
    assert fake_ws.closed is True


async def test_remote_terminal_send_command_parses_queue_output(terminal_session):
    module, session = terminal_session
    session.rx_queue.put_nowait(("output", "hello world\n"))

    payload = _payload(await _call(module, "send_command", {"sn": SN, "command": "echo hello", "timeout": 0.01}))
    assert payload["success"] is True
    assert payload["output"] == "hello world"
    assert session.ws.sent and session.ws.sent[0].startswith(b"\x00")


async def test_remote_terminal_send_raw_keeps_bytes(terminal_session):
    module, session = terminal_session
    payload = _payload(await _call(module, "send_raw", {"sn": SN, "data": "x"}))
    assert payload["success"] is True
    assert session.ws.sent[-1] == b"\x00x"


async def test_remote_terminal_receive_output_wraps_queue(terminal_session):
    module, session = terminal_session
    session.rx_queue.put_nowait(("output", "abc"))
    payload = _payload(await _call(module, "receive_output", {"sn": SN, "timeout": 0.01}))
    assert payload["success"] is True
    assert {"type": "output", "data": "abc"} in payload["outputs"]


async def test_remote_terminal_resize_sends_winsize(terminal_session):
    module, session = terminal_session
    payload = _payload(await _call(module, "resize_terminal", {"sn": SN, "cols": 120, "rows": 40}))
    assert payload == {"success": True, "sn": SN, "cols": 120, "rows": 40}
    assert json.loads(session.ws.sent[-1]) == {"type": "winsize", "cols": 120, "rows": 40}


async def test_remote_terminal_buffer_and_status_tools(terminal_session):
    module, session = terminal_session
    session.output_buffer.extend(["a", "b", "c"])

    payload = _payload(await _call(module, "get_buffer", {"sn": SN, "lines": 2}))
    assert payload["buffer"] == ["b", "c"]

    payload = _payload(await _call(module, "clear_buffer", {"sn": SN}))
    assert payload["success"] is True
    assert session.output_buffer == []

    payload = _payload(await _call(module, "get_session_status", {"sn": SN}))
    assert payload["is_logged_in"] is True
    assert payload["buffer_size"] == 0


async def test_remote_terminal_set_ws_base_url(terminal_session):
    module, _ = terminal_session
    payload = _payload(await _call(module, "set_ws_base_url", {"base_url": "wss://example.test:10000/"}))
    assert payload == {"success": True, "base_url": "wss://example.test:10000"}
    assert module.WS_BASE_URL == "wss://example.test:10000"


async def test_remote_terminal_interactive_session(terminal_session, monkeypatch):
    module, session = terminal_session

    async def fake_receive_output(sn, timeout=2.0):
        return [{"type": "output", "data": "ok\n"}]

    monkeypatch.setattr(module, "_receive_output", fake_receive_output)
    payload = _payload(
        await _call(module, "interactive_session", {"sn": SN, "commands": ["ls", "pwd"], "delay": 0.0})
    )
    assert payload["success"] is True
    assert payload["total_commands"] == 2
    assert all(item["output"] == "ok" for item in payload["results"])
    assert len(session.ws.sent) == 2


async def test_remote_terminal_execute_with_retry(terminal_session, monkeypatch):
    module, _ = terminal_session

    async def fake_receive_output(sn, timeout=2.0):
        return [{"type": "output", "data": "done\n"}]

    monkeypatch.setattr(module, "_receive_output", fake_receive_output)
    payload = _payload(
        await _call(module, "execute_with_retry", {"sn": SN, "command": "make", "max_retries": 2, "timeout": 0.01})
    )
    assert payload["success"] is True
    assert payload["command_success"] is True
    assert payload["attempts"] == 1


async def test_remote_terminal_wait_for_prompt(terminal_session):
    module, session = terminal_session
    session.rx_queue.put_nowait(("output", "user@host:~$ "))
    payload = _payload(await _call(module, "wait_for_prompt", {"sn": SN, "timeout": 1.0}))
    assert payload["success"] is True
    assert payload["prompt"] == "user@host:~$"


async def test_remote_terminal_strip_ansi(terminal_session):
    module, _ = terminal_session
    payload = _payload(await _call(module, "strip_ansi", {"text": "\x1b[31mred\x1b[0m"}))
    assert payload["cleaned_text"] == "red"
