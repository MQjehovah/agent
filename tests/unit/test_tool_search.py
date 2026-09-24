"""渐进披露(工具搜索)测试。

覆盖: 模式/阈值解析与 auto 边界、检索打分/limit/空查询、hint 文案、
工具注入组装(核心+激活, auto/always/off)、可用性过滤、激活持久化
(session_meta 往返/损坏容错/不可用回退内存)、search_tools 执行。
"""
import logging
import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest

from agent.core import Agent, RunContext, _current_run
from agent.tool_search import (
    SEARCH_TOOLS_NAME,
    ToolActivationStore,
    ToolSearchEntry,
    ToolSearchMode,
    is_remote_tool,
    parse_tool_search_mode,
    parse_tool_search_threshold,
    progressive_hint,
    search_remote_tools,
    should_use_progressive,
    tool_search_mode_from_env,
    tool_search_threshold_from_env,
)
from storage.storage import Storage
from tools.search_tools import SearchToolsTool


@pytest.fixture(autouse=True)
def _clean_activation_cache():
    """测试间隔离进程内激活缓存(模块级)。"""
    ToolActivationStore.reset_cache()
    yield
    ToolActivationStore.reset_cache()


# ===== 1. 模式解析 / 阈值边界 / auto =====

@pytest.mark.parametrize("value,expected", [
    ("auto", ToolSearchMode.AUTO),
    ("always", ToolSearchMode.ALWAYS),
    ("off", ToolSearchMode.OFF),
    (" ALWAYS ", ToolSearchMode.ALWAYS),
    ("Off", ToolSearchMode.OFF),
    (None, ToolSearchMode.AUTO),
    ("", ToolSearchMode.AUTO),
    ("bogus", ToolSearchMode.AUTO),
    ("progressive", ToolSearchMode.AUTO),
])
def test_parse_tool_search_mode(value, expected):
    assert parse_tool_search_mode(value) is expected


def test_parse_tool_search_mode_invalid_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="agent.tool_search"):
        assert parse_tool_search_mode("whatever") is ToolSearchMode.AUTO
    assert any("AGENT_TOOL_SEARCH" in r.message for r in caplog.records)


@pytest.mark.parametrize("value,expected", [
    (None, 40),
    ("", 40),
    ("40", 40),
    ("10", 10),
    ("0", 0),
    ("abc", 40),
    ("-1", 40),
    ("3.5", 40),
])
def test_parse_tool_search_threshold(value, expected):
    assert parse_tool_search_threshold(value) == expected


def test_tool_search_env_readers(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "always")
    monkeypatch.setenv("AGENT_TOOL_SEARCH_THRESHOLD", "7")
    assert tool_search_mode_from_env() is ToolSearchMode.ALWAYS
    assert tool_search_threshold_from_env() == 7
    monkeypatch.delenv("AGENT_TOOL_SEARCH")
    monkeypatch.delenv("AGENT_TOOL_SEARCH_THRESHOLD")
    assert tool_search_mode_from_env() is ToolSearchMode.AUTO
    assert tool_search_threshold_from_env() == 40


@pytest.mark.parametrize("mode,remote,expected", [
    (ToolSearchMode.AUTO, 40, False),   # auto 边界: 等于阈值不触发
    (ToolSearchMode.AUTO, 41, True),    # 41 > 40 触发
    (ToolSearchMode.AUTO, 200, True),
    (ToolSearchMode.OFF, 999, False),
    (ToolSearchMode.ALWAYS, 0, True),
])
def test_should_use_progressive_boundaries(mode, remote, expected):
    assert should_use_progressive(mode, remote) is expected
    assert should_use_progressive(mode.value, remote) is expected


def test_is_remote_tool_by_source_and_name():
    assert is_remote_tool("device_reboot", "mcp") is True
    assert is_remote_tool("send_message_to_dingtalk", "plugin") is True
    assert is_remote_tool("market__x", "market") is True
    assert is_remote_tool("file", "builtin") is False
    assert is_remote_tool("skill", "skill") is False
    # 来源缺失时按暴露名前缀兜底
    assert is_remote_tool("platform__weather__forecast") is True
    assert is_remote_tool("mcp__device__reboot") is True
    assert is_remote_tool("market:web_search") is True
    assert is_remote_tool("file") is False


# ===== 2. 检索打分 / limit / 空查询 / 文案 =====

def _entry(name, description="", source=""):
    return ToolSearchEntry(name=name, description=description, source=source)


def test_search_remote_tools_scoring_order():
    entries = [
        _entry("reboot", "重启设备"),
        _entry("device_reboot", "重启远程设备"),
        _entry("other", "重启任务", source="reboot"),
        _entry("desc_hit", "reboot 说明"),
        _entry("no_match", "无"),
    ]
    hits, text = search_remote_tools(entries, "reboot")
    assert [h.name for h in hits] == ["reboot", "device_reboot", "other", "desc_hit"]
    assert [h.score for h in hits] == [100, 10, 5, 2]
    assert "找到 4 个" in text
    assert "已激活" in text


def test_search_remote_tools_multi_term_accumulates():
    entries = [
        _entry("device_reboot", "重启远程设备", "remote_terminal"),
        _entry("device_status", "查看设备状态", "device_api"),
    ]
    hits, _ = search_remote_tools(entries, "device reboot")
    assert hits[0].name == "device_reboot"
    assert hits[0].score == 20  # 两个词都命中名称包含(+10+10)
    assert [h.name for h in hits] == ["device_reboot", "device_status"]


def test_search_remote_tools_tie_breaks_by_name_asc():
    entries = [_entry("bbb", "命中词"), _entry("aaa", "命中词")]
    hits, _ = search_remote_tools(entries, "命中词")
    assert [h.name for h in hits] == ["aaa", "bbb"]
    assert hits[0].score == hits[1].score == 2


def test_search_remote_tools_limit_clamp():
    entries = [_entry(f"tool_{i:02d}", "同名描述") for i in range(25)]
    hits, _ = search_remote_tools(entries, "同名描述", limit=99)
    assert len(hits) == 20
    hits, _ = search_remote_tools(entries, "同名描述", limit=0)
    assert len(hits) == 1
    hits, _ = search_remote_tools(entries, "同名描述", limit="bad")
    assert len(hits) == 8
    hits, _ = search_remote_tools(entries, "同名描述", limit=5)
    assert [h.name for h in hits] == ["tool_00", "tool_01", "tool_02", "tool_03", "tool_04"]


def test_search_remote_tools_empty_query_and_no_hit():
    entries = [_entry("file", "读文件")]
    hits, text = search_remote_tools(entries, "   ")
    assert hits == []
    assert "关键词" in text
    hits, text = search_remote_tools(entries, "device")
    assert hits == []
    assert "未找到匹配「device」" in text
    assert "换关键词" in text


def test_progressive_hint_lists_active_or_none():
    hinted = progressive_hint(["mcp_a", "mcp_b"])
    assert "search_tools" in hinted
    assert "已激活：mcp_a、mcp_b" in hinted
    assert "已激活：无" in progressive_hint([])
    assert "已激活：无" in progressive_hint([""])


# ===== 3. 激活持久化(session_meta 往返/损坏/回退) =====

def test_storage_active_tools_roundtrip(tmp_path):
    storage = Storage(str(tmp_path))
    assert storage.get_active_tools("web:1:c1") == []
    storage.set_active_tools("web:1:c1", ["mcp_a", "mcp_b"])
    assert storage.get_active_tools("web:1:c1") == ["mcp_a", "mcp_b"]
    storage.set_active_tools("web:1:c1", ["mcp_b"])
    assert storage.get_active_tools("web:1:c1") == ["mcp_b"]
    assert storage.delete_active_tools("web:1:c1") is True
    assert storage.get_active_tools("web:1:c1") == []
    assert storage.delete_active_tools("web:1:c1") is False


def test_storage_active_tools_corruption_tolerated(tmp_path):
    storage = Storage(str(tmp_path))
    storage.set_active_tools("web:1:c1", ["mcp_a"])
    db = tmp_path / "data.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE session_meta SET active_tools = ? WHERE session_id = ?",
                     ("{not-json", "web:1:c1"))
        conn.commit()
    assert storage.get_active_tools("web:1:c1") == []
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE session_meta SET active_tools = ? WHERE session_id = ?",
                     ('{"a": 1}', "web:1:c1"))
        conn.commit()
    assert storage.get_active_tools("web:1:c1") == []


def test_save_session_meta_preserves_active_tools(tmp_path):
    storage = Storage(str(tmp_path))
    storage.set_active_tools("web:1:c1", ["mcp_a"])
    storage.save_session_meta("web:1:c1", "历史摘要")
    assert storage.get_session_meta("web:1:c1")["last_summary"] == "历史摘要"
    assert storage.get_active_tools("web:1:c1") == ["mcp_a"]


def test_activation_store_storage_roundtrip(tmp_path):
    storage = Storage(str(tmp_path))
    ToolActivationStore.activate("web:1:c1", ["mcp_a"], storage)
    ToolActivationStore.activate("web:1:c1", ["mcp_b", "mcp_a"], storage)
    assert ToolActivationStore.load("web:1:c1", storage) == frozenset({"mcp_a", "mcp_b"})
    # 模拟重启: 清进程内缓存后从 session_meta 恢复
    ToolActivationStore.reset_cache()
    assert ToolActivationStore.load("web:1:c1", storage) == frozenset({"mcp_a", "mcp_b"})
    assert storage.get_active_tools("web:1:c1") == ["mcp_a", "mcp_b"]


def test_activation_store_memory_fallback_when_storage_unavailable():
    activated = ToolActivationStore.activate("web:1:c2", ["mcp_a"], storage=None)
    assert activated == frozenset({"mcp_a"})
    assert ToolActivationStore.load("web:1:c2", storage=None) == frozenset({"mcp_a"})
    assert ToolActivationStore.load("web:1:c3", storage=None) == frozenset()
    assert ToolActivationStore.load("", storage=None) == frozenset()
    assert ToolActivationStore.activate("", ["mcp_a"], storage=None) == frozenset()


class _BrokenStorage:
    def get_active_tools(self, session_id):
        raise RuntimeError("db down")

    def set_active_tools(self, session_id, names):
        raise RuntimeError("db down")


def test_activation_store_storage_errors_do_not_break():
    assert ToolActivationStore.load("web:1:c4", _BrokenStorage()) == frozenset()
    # load 失败不缓存, 本次 activate 仍内存生效
    assert ToolActivationStore.activate("web:1:c4", ["mcp_a"], _BrokenStorage()) == frozenset({"mcp_a"})
    assert ToolActivationStore.load("web:1:c4", None) == frozenset({"mcp_a"})


# ===== 4. 工具注入组装(真实 Agent + 假工具集) =====

def _tool_def(name, description="desc"):
    return {"type": "function",
            "function": {"name": name, "description": description, "parameters": {}}}


class _FakeRegistry:
    def __init__(self, defs):
        self._defs = list(defs)

    def get_tool_definitions(self):
        return list(self._defs)

    def get_tool(self, name):
        return None

    def list_tools(self):
        return [d["function"]["name"] for d in self._defs]


class _FakeMCP:
    def __init__(self, defs, unavailable=()):
        self.tool_defs = list(defs)
        self.unavailable = set(unavailable)
        self.reserved_names = set()

    def set_reserved_names(self, names):
        self.reserved_names = set(names)

    def tool_server(self, name):
        return "srv"

    def is_tool_available(self, name):
        return name not in self.unavailable


class _FakePlugin:
    def __init__(self, name, defs):
        self.name = name
        self.enabled = True
        self._defs = list(defs)

    def get_tool_defs(self):
        return list(self._defs)


class _FakePluginManager:
    def __init__(self, plugins):
        self.plugins = {p.name: p for p in plugins}


CORE_COUNT = 20
REMOTE_COUNT = 60


def _make_agent(tmp_path, core=CORE_COUNT, remote=REMOTE_COUNT, plugins=()):
    agent = Agent(workspace=str(tmp_path), client=SimpleNamespace(model="test"))
    core_names = [f"core_{i}" for i in range(core - 1)] + [SEARCH_TOOLS_NAME]
    agent.tool_registry = _FakeRegistry([_tool_def(n) for n in core_names])
    agent.mcp = _FakeMCP([_tool_def(f"mcp_tool_{i}") for i in range(remote)])
    agent.plugin_manager = _FakePluginManager(plugins)
    return agent


def _def_names(defs):
    return [d["function"]["name"] for d in defs]


def _run_tool_defs(agent, conversation_id, run_id="r1"):
    rc = RunContext(conversation_id=conversation_id, run_id=run_id)
    token = _current_run.set(rc)
    try:
        return agent.tool_defs
    finally:
        _current_run.reset(token)


def test_auto_progressive_injects_core_plus_search_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "auto")
    monkeypatch.setenv("AGENT_TOOL_SEARCH_THRESHOLD", "40")
    agent = _make_agent(tmp_path)
    names = _def_names(_run_tool_defs(agent, "web:1:conv"))
    assert len(names) == CORE_COUNT
    assert SEARCH_TOOLS_NAME in names
    assert not any(n.startswith("mcp_tool_") for n in names)


def test_auto_progressive_at_threshold_keeps_full_list(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "auto")
    monkeypatch.setenv("AGENT_TOOL_SEARCH_THRESHOLD", "60")
    agent = _make_agent(tmp_path)
    names = _def_names(_run_tool_defs(agent, "web:1:conv"))
    assert len(names) == CORE_COUNT + REMOTE_COUNT


def test_auto_progressive_activation_sticks_next_round(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "auto")
    agent = _make_agent(tmp_path)
    ToolActivationStore.activate("web:1:conv", ["mcp_tool_3", "mcp_tool_5"], None)
    names = _def_names(_run_tool_defs(agent, "web:1:conv"))
    assert len(names) == CORE_COUNT + 2
    assert names[CORE_COUNT:] == ["mcp_tool_3", "mcp_tool_5"]
    # 其他对话不受影响(激活按对话根隔离)
    other = _def_names(_run_tool_defs(agent, "web:2:other"))
    assert len(other) == CORE_COUNT


def test_off_mode_keeps_full_list(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "off")
    agent = _make_agent(tmp_path)
    ToolActivationStore.activate("web:1:conv", ["mcp_tool_3"], None)
    names = _def_names(_run_tool_defs(agent, "web:1:conv"))
    assert len(names) == CORE_COUNT + REMOTE_COUNT
    assert names[0] == "core_0"
    assert names[CORE_COUNT - 1] == SEARCH_TOOLS_NAME
    assert names[CORE_COUNT] == "mcp_tool_0"


def test_always_mode_progressive_even_with_zero_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "always")
    agent = _make_agent(tmp_path, remote=0)
    names = _def_names(_run_tool_defs(agent, "web:1:conv"))
    assert len(names) == CORE_COUNT
    assert SEARCH_TOOLS_NAME in names


def test_progressive_filters_unavailable_activated_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "always")
    agent = _make_agent(tmp_path)
    agent.mcp.unavailable = {"mcp_tool_3"}
    ToolActivationStore.activate("web:1:conv", ["mcp_tool_3", "mcp_tool_5"], None)
    names = _def_names(_run_tool_defs(agent, "web:1:conv"))
    assert names[CORE_COUNT:] == ["mcp_tool_5"]
    # 持久化不清理: 下次可用自动恢复
    agent.mcp.unavailable = set()
    names = _def_names(_run_tool_defs(agent, "web:1:conv", run_id="r2"))
    assert names[CORE_COUNT:] == ["mcp_tool_3", "mcp_tool_5"]


def test_plugin_tools_are_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "always")
    plugin = _FakePlugin("dingtalk", [_tool_def("send_message_to_dingtalk")])
    agent = _make_agent(tmp_path, remote=0, plugins=[plugin])
    names = _def_names(_run_tool_defs(agent, "web:1:conv"))
    assert "send_message_to_dingtalk" not in names
    ToolActivationStore.activate("web:1:conv", ["send_message_to_dingtalk"], None)
    names = _def_names(_run_tool_defs(agent, "web:1:conv", run_id="r2"))
    assert "send_message_to_dingtalk" in names


def test_tool_defs_without_conversation_root_is_full(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "always")
    agent = _make_agent(tmp_path)
    names = _def_names(agent.tool_defs)  # run 之外(无对话根)
    assert len(names) == CORE_COUNT + REMOTE_COUNT


def test_tool_injection_log_dedup(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "auto")
    agent = _make_agent(tmp_path)
    rc = RunContext(conversation_id="web:1:conv", run_id="r1")
    token = _current_run.set(rc)
    try:
        with caplog.at_level(logging.INFO, logger="agent.agent"):
            _ = agent.tool_defs
            _ = agent.tool_defs
    finally:
        _current_run.reset(token)
    msgs = [r.message for r in caplog.records if "工具注入" in r.message]
    assert len(msgs) == 1
    assert msgs[0] == "工具注入: 核心 20 + 激活 0 + search_tools（渐进 auto）"


# ===== 5. 系统提示渐进说明 =====

def test_progressive_hint_appended_to_system_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "auto")
    agent = _make_agent(tmp_path)
    agent.system_static = "STATIC"
    agent.system_dynamic = "DYNAMIC"
    agent.system_prompt = "STATICDYNAMIC"
    ToolActivationStore.activate("web:1:conv", ["mcp_tool_1"], None)
    rc = RunContext(conversation_id="web:1:conv")
    agent._apply_progressive_hint(rc)
    assert rc.system_static == "STATIC"           # 静态前缀保持可缓存
    assert "工具搜索" in rc.system_dynamic
    assert "mcp_tool_1" in rc.system_prompt
    # 实例完整提示含额外追加(子代理技能指引)时不丢内容
    agent.system_prompt = "STATICDYNAMIC\n技能指引"
    rc2 = RunContext(conversation_id="web:1:conv")
    agent._apply_progressive_hint(rc2)
    assert "技能指引" in rc2.system_prompt
    assert rc2.system_prompt.startswith("STATIC")


def test_progressive_hint_not_applied_in_off_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "off")
    agent = _make_agent(tmp_path)
    rc = RunContext(conversation_id="web:1:conv")
    agent._apply_progressive_hint(rc)
    assert rc.system_prompt == ""
    assert "工具搜索" not in rc.system_dynamic


# ===== 6. search_tools 执行 =====

async def test_search_tools_execute_activates_hits():
    captured = {}

    def activator(names):
        captured["names"] = list(names)
        return set(names)

    entries = [
        ToolSearchEntry("device_reboot", "重启远程设备", "remote_terminal"),
        ToolSearchEntry("device_status", "查看设备状态", "device_api"),
    ]
    tool = SearchToolsTool(entries_provider=lambda: entries, activator=activator)
    out = await tool.execute(query="reboot")
    assert captured["names"] == ["device_reboot"]
    assert "device_reboot" in out
    assert "已激活" in out
    assert tool.name == SEARCH_TOOLS_NAME
    assert tool.parameters["required"] == ["query"]


async def test_search_tools_execute_no_hit_does_not_activate():
    called = []
    tool = SearchToolsTool(
        entries_provider=lambda: [ToolSearchEntry("device_reboot", "重启设备")],
        activator=lambda names: called.append(names) or set(names),
    )
    out = await tool.execute(query="不存在的工具")
    assert "未找到匹配" in out
    assert called == []


async def test_search_tools_execute_empty_query_and_missing_source():
    tool = SearchToolsTool(entries_provider=lambda: [], activator=lambda names: set(names))
    assert "query" in await tool.execute(query="   ")
    assert "query" in await tool.execute()
    bare = SearchToolsTool()
    assert "未配置远程工具源" in await bare.execute(query="device")


async def test_search_tools_execute_without_activation_keeps_promise_honest():
    tool = SearchToolsTool(
        entries_provider=lambda: [ToolSearchEntry("device_reboot", "重启设备")],
        activator=lambda names: set(),  # 无对话根/激活不可用
    )
    out = await tool.execute(query="reboot")
    assert "未能激活" in out


# ===== 7. MCP 可用性判定(注入前过滤依据) 与内置注册 =====

def test_mcp_manager_is_tool_available_local_and_platform():
    from mcps.manager import MCPManager

    mgr = MCPManager("")
    server = SimpleNamespace(is_connected=True)
    mgr.servers["s1"] = server
    mgr._tool_to_server["t1"] = "s1"
    assert mgr.is_tool_available("t1") is True
    server.is_connected = False
    assert mgr.is_tool_available("t1") is False
    assert mgr.is_tool_available("unknown_tool") is False

    class _FakePlatform:
        def has_tool(self, name):
            return name == "platform__cap__t"

    mgr.platform = _FakePlatform()
    assert mgr.is_tool_available("platform__cap__t") is True
    assert mgr.is_tool_available("platform__cap__x") is False


def test_search_tools_registered_in_builtin_registry():
    from tools import ToolRegistry

    registry = ToolRegistry()
    registry.auto_discover()
    tool = registry.get_tool(SEARCH_TOOLS_NAME)
    assert tool is not None
    assert tool.description


async def test_agent_search_tools_wires_provider_and_activator(tmp_path, monkeypatch):
    """端到端(装配层): Agent 注入的检索源/激活回调把命中写进对话根, 下一轮注入即带上。"""
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "auto")
    agent = _make_agent(tmp_path)
    tool = SearchToolsTool()
    tool.configure(entries_provider=agent._remote_tool_entries,
                   activator=agent._activate_remote_tools)
    rc = RunContext(conversation_id="web:1:conv", run_id="r1")
    token = _current_run.set(rc)
    try:
        out = await tool.execute(query="mcp_tool_37")
        assert "mcp_tool_37" in out
        assert ToolActivationStore.load("web:1:conv") == frozenset({"mcp_tool_37"})
        names = _def_names(agent.tool_defs)
    finally:
        _current_run.reset(token)
    assert names[CORE_COUNT:] == ["mcp_tool_37"]
