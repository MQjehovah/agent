"""W2b 测试: MCP 工具级风险(注解→风险) + PermissionChecker 风险接入 + 敏感标记。

不真连 stdio: 用 monkeypatch 的 fake stdio_client + FakeSessionCtx 模拟连接(含注解);
权限矩阵用 fake risk_resolver; resolver=None 时与旧行为逐项回归对比。
"""
import json
import os
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from mcp.types import ToolAnnotations

import mcps.manager as manager
from security.permissions import PermissionChecker, PermissionConfig, PermissionMode

NON_MCP = object()


# ===== 1. 注解 → 风险映射(classify_tool_risk) =====

@pytest.mark.parametrize("annotations,expected", [
    (None, "unknown"),
    (ToolAnnotations(readOnlyHint=True, destructiveHint=False), "read"),
    (ToolAnnotations(readOnlyHint=False, destructiveHint=True), "destructive"),
    (ToolAnnotations(readOnlyHint=False, destructiveHint=False), "write"),
    (ToolAnnotations(readOnlyHint=False), "write"),
    (ToolAnnotations(), "write"),
    (SimpleNamespace(read_only_hint=True), "read"),
    (SimpleNamespace(read_only_hint=False, destructive_hint=True), "destructive"),
    (SimpleNamespace(read_only_hint=False, destructive_hint=False), "write"),
    ({"readOnlyHint": True}, "read"),
    ({"destructiveHint": True}, "destructive"),
    ({"read_only_hint": True}, "read"),
    ({"destructive_hint": True}, "destructive"),
    ({"read_only_hint": False, "destructive_hint": False}, "write"),
    (SimpleNamespace(read_only_hint="yes"), "write"),  # 非 bool 不算只读
])
def test_classify_tool_risk(annotations, expected):
    assert manager.classify_tool_risk(annotations) == expected


# ===== 2. 连接期捕获 + risk_overrides 覆盖 + manager 暴露映射 =====

def _install_fake_stdio(monkeypatch, tools_by_file: dict[str, list]):
    """fake stdio: 按 args 文件名返回带 annotations 的工具对象列表。"""

    class FakeSessionCtx:
        def __init__(self, read_stream, write_stream):
            self._tools = getattr(read_stream, "tools", [])

        async def __aenter__(self):
            return SimpleNamespace(
                initialize=AsyncMock(),
                list_tools=AsyncMock(return_value=SimpleNamespace(tools=self._tools)),
            )

        async def __aexit__(self, *exc_info):
            return False

    @asynccontextmanager
    async def fake_stdio_client(server, errlog=None):
        marker = os.path.basename(server.args[-1]) if server.args else ""
        yield (SimpleNamespace(tools=tools_by_file.get(marker, [])), object())

    monkeypatch.setattr(manager, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(manager, "ClientSession", FakeSessionCtx)


def _tool(name: str, annotations=None):
    return SimpleNamespace(name=name, description=f"工具 {name}",
                           input_schema={"type": "object"}, annotations=annotations)


def _write_config(tmp_path, entries: list[dict]) -> str:
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return str(path)


async def test_connect_captures_annotations_and_applies_overrides(tmp_path, monkeypatch):
    _install_fake_stdio(monkeypatch, {"w2b_risk.py": [
        _tool("r", ToolAnnotations(readOnlyHint=True)),
        _tool("d", ToolAnnotations(readOnlyHint=False, destructiveHint=True)),
        _tool("w", ToolAnnotations(readOnlyHint=False, destructiveHint=False)),
        _tool("n"),
    ]})
    cfg = _write_config(tmp_path, [
        {"name": "w2b_risk", "enabled": True, "command": "python", "args": ["w2b_risk.py"],
         "risk_overrides": {"d": "read", "r": "destructive", "n": "bad-value"}},
    ])
    mgr = manager.MCPManager(cfg)
    await mgr.connect()
    try:
        conn = mgr.servers["w2b_risk"]
        assert conn.risk_of("r") == "destructive"  # overrides 覆盖注解
        assert conn.risk_of("d") == "read"
        assert conn.risk_of("w") == "write"
        assert conn.risk_of("n") == "unknown"      # 非法 override 忽略, 无注解保持 unknown
        assert conn.risk_of("不存在") == "unknown"

        # manager 暴露名映射随 _rebuild_tool_defs 重建
        assert mgr.tool_risk("r") == "destructive"
        assert mgr.tool_risk("d") == "read"
        assert mgr.tool_risk("n") == "unknown"
        assert mgr.tool_server("r") == "w2b_risk"
        assert mgr.tool_raw("r") == "r"
        # 非 MCP 工具名 → None(权限层/敏感层据此区分)
        assert mgr.tool_risk("shell") is None
        assert mgr.tool_server("shell") is None
        assert mgr.tool_raw("shell") is None

        # 断开后映射整体重建, 不残留
        await mgr.disconnect_server("w2b_risk")
        assert mgr.tool_risk("r") is None
        await mgr.connect_server({"name": "w2b_risk", "enabled": True,
                                  "command": "python", "args": ["w2b_risk.py"]})
        assert mgr.tool_risk("r") == "read"  # 新连接无 overrides, 回到注解
    finally:
        await mgr.close()


async def test_risk_overrides_requires_dict(tmp_path, monkeypatch):
    _install_fake_stdio(monkeypatch, {"w2b_bad_ov.py": [_tool("t", ToolAnnotations(readOnlyHint=True))]})
    cfg = _write_config(tmp_path, [
        {"name": "w2b_bad_ov", "enabled": True, "command": "python", "args": ["w2b_bad_ov.py"],
         "risk_overrides": ["not-a-dict"]},
    ])
    mgr = manager.MCPManager(cfg)
    await mgr.connect()
    try:
        assert mgr.tool_risk("t") == "read"  # 非法配置不阻断, 回退注解
    finally:
        await mgr.close()


async def test_prefixed_duplicate_tool_keeps_risk(tmp_path, monkeypatch):
    _install_fake_stdio(monkeypatch, {
        "w2b_a.py": [_tool("get", ToolAnnotations(readOnlyHint=True))],
        "w2b_b.py": [_tool("get", ToolAnnotations(readOnlyHint=False, destructiveHint=True))],
    })
    cfg = _write_config(tmp_path, [
        {"name": "w2b_a", "enabled": True, "command": "python", "args": ["w2b_a.py"]},
        {"name": "w2b_b", "enabled": True, "command": "python", "args": ["w2b_b.py"]},
    ])
    mgr = manager.MCPManager(cfg)
    await mgr.connect()
    try:
        assert mgr.tool_risk("get") == "read"
        assert mgr.tool_risk("w2b_b__get") == "destructive"
        assert mgr.tool_server("w2b_b__get") == "w2b_b"
        assert mgr.tool_raw("w2b_b__get") == "get"
    finally:
        await mgr.close()


# ===== 3. PermissionChecker 风险矩阵 =====

RISK_BY_NAME = {
    "mcp_read": "read",
    "mcp_write": "write",
    "mcp_destructive": "destructive",
    "mcp_unknown": "unknown",
}


def _checker(mode: PermissionMode, resolver=None):
    return PermissionChecker(PermissionConfig(mode=mode), risk_resolver=resolver)


def _risk_resolver(name: str):
    return RISK_BY_NAME.get(name)


@pytest.mark.parametrize("name,allowed,denied", [
    ("mcp_read", True, False),
    ("mcp_unknown", True, False),
    ("mcp_write", False, True),
    ("mcp_destructive", False, True),
])
def test_plan_mode_denies_mcp_write_risk(name, allowed, denied):
    result = _checker(PermissionMode.PLAN, _risk_resolver).check(name, {})
    assert result.allowed is allowed
    if denied:
        assert result.reason == f"PLAN 模式禁止执行写类工具: {name}"
    else:
        assert result.reason == ""


@pytest.mark.parametrize("name,allowed,confirm", [
    ("mcp_read", True, False),
    ("mcp_unknown", True, False),
    ("mcp_write", True, False),         # SMART 仅 destructive 需要确认
    ("mcp_destructive", True, True),
])
def test_smart_mode_confirms_only_destructive(name, allowed, confirm):
    result = _checker(PermissionMode.SMART, _risk_resolver).check(name, {})
    assert result.allowed is allowed
    assert result.reason == ("需要用户确认" if confirm else "")


@pytest.mark.parametrize("name,confirm", [
    ("mcp_read", False),
    ("mcp_unknown", False),
    ("mcp_write", True),
    ("mcp_destructive", True),
])
def test_default_mode_confirms_mcp_writes(name, confirm):
    result = _checker(PermissionMode.DEFAULT, _risk_resolver).check(name, {})
    assert result.allowed is True
    assert result.reason == ("需要用户确认" if confirm else "")


@pytest.mark.parametrize("name", ["mcp_read", "mcp_write", "mcp_destructive", "mcp_unknown"])
def test_auto_mode_allows_all_mcp_risk(name):
    result = _checker(PermissionMode.AUTO, _risk_resolver).check(name, {})
    assert result.allowed is True
    assert result.reason == ""


def test_resolver_exception_treated_as_non_mcp():
    def broken(_name):
        raise RuntimeError("boom")

    result = _checker(PermissionMode.PLAN, broken).check("mcp_write", {})
    assert result.allowed is True  # 解析异常不误杀


def test_resolver_garbage_treated_as_non_mcp():
    result = _checker(PermissionMode.PLAN, lambda _n: "super-danger").check("mcp_write", {})
    assert result.allowed is True


# ===== 4. resolver=None 行为与旧版完全一致(回归) =====

REGRESSION_CASES = [
    ("file", {"operation": "read"}),
    ("file", {"operation": "write", "path": "workspace/a.txt"}),
    ("shell", {"command": "ls -la"}),
    ("shell", {"command": "rm -rf /"}),
    ("shell", {"command": "echo hi"}),
    ("edit", {"path": "workspace/a.txt"}),
    ("mcp_read", {}),
    ("mcp_destructive", {}),
    ("unknown_tool", {}),
]


@pytest.mark.parametrize("mode", [PermissionMode.DEFAULT, PermissionMode.SMART,
                                  PermissionMode.PLAN, PermissionMode.AUTO])
def test_resolver_none_matches_legacy_behavior(mode):
    legacy = PermissionChecker(PermissionConfig(mode=mode))
    same = PermissionChecker(PermissionConfig(mode=mode), risk_resolver=None)
    none_resolver = PermissionChecker(PermissionConfig(mode=mode), risk_resolver=lambda _n: None)
    for name, args in REGRESSION_CASES:
        r0 = legacy.check(name, args)
        r1 = same.check(name, args)
        r2 = none_resolver.check(name, args)
        assert (r1.allowed, r1.reason) == (r0.allowed, r0.reason), (mode, name)
        assert (r2.allowed, r2.reason) == (r0.allowed, r0.reason), (mode, name)


# ===== 5. destructive MCP 工具 → 当前 run 敏感标记 =====

class _FakeMCP:
    def __init__(self, risk):
        self.risk = risk

    def tool_risk(self, name):
        return self.risk


def test_destructive_mcp_tool_marks_run_sensitive():
    from agent.core import RunContext, _current_run
    from agent.executor import _mark_run_sensitive_if_hit

    agent = SimpleNamespace(mcp=_FakeMCP("destructive"))
    rc = RunContext(user_id="dingtalk:7", run_id="rid-destr")
    tok = _current_run.set(rc)
    try:
        _mark_run_sensitive_if_hit(agent, "device_factory_reset")
    finally:
        _current_run.reset(tok)
    assert rc.sensitive_hit is True


@pytest.mark.parametrize("risk", ["read", "write", "unknown", None])
def test_non_destructive_mcp_tool_not_marked(risk):
    from agent.core import RunContext, _current_run
    from agent.executor import _mark_run_sensitive_if_hit

    agent = SimpleNamespace(mcp=_FakeMCP(risk))
    rc = RunContext(user_id="web:7", run_id="rid-non")
    tok = _current_run.set(rc)
    try:
        _mark_run_sensitive_if_hit(agent, "device_status")
    finally:
        _current_run.reset(tok)
    assert rc.sensitive_hit is False


def test_sensitive_mark_requires_active_run():
    from agent.core import RunContext, _current_run
    from agent.executor import _mark_run_sensitive_if_hit

    agent = SimpleNamespace(mcp=_FakeMCP("destructive"))
    rc = RunContext(user_id="web:7")  # 无 run_id(非活跃 run)
    tok = _current_run.set(rc)
    try:
        _mark_run_sensitive_if_hit(agent, "device_factory_reset")
    finally:
        _current_run.reset(tok)
    assert rc.sensitive_hit is False


def test_sensitive_config_list_still_applies_without_mcp():
    from agent.core import RunContext, _current_run
    from agent.executor import _mark_run_sensitive_if_hit

    agent = SimpleNamespace(mcp=None)
    rc = RunContext(user_id="web:7", run_id="rid-list")
    tok = _current_run.set(rc)
    try:
        _mark_run_sensitive_if_hit(agent, "execute_query")
    finally:
        _current_run.reset(tok)
    assert rc.sensitive_hit is True
