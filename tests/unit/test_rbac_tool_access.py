"""RBAC 只读限定条目(file:read 等) + PermissionChecker.classify_access 归类测试。

覆盖:
- allowed_tools 三类条目: ``*`` 通配 / ``file`` 旧语义(读写均放行) / ``file:read`` 只读;
- classify_access: file 各 operation、shell 读前缀/写命令、MCP 风险映射(unknown→write)
  与 resolver 缺失时的内置 write_tools 兜底;
- executor 集成: default 角色配 file:read 时 file read 放行、file write 返回权限文案。
"""
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest

from agent.core import RunContext, _current_run
from hooks import HookEvent
from security.permissions import PermissionChecker, PermissionConfig, PermissionMode
from security.rbac import RBACManager
from storage.storage import Storage

RISK_BY_NAME = {
    "mcp_read": "read",
    "mcp_write": "write",
    "mcp_destructive": "destructive",
    "mcp_unknown": "unknown",
}


@pytest.fixture
def storage(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return Storage(str(ws))


@pytest.fixture
def rbac(storage):
    return RBACManager(storage)


def _checker(risk_resolver=None):
    return PermissionChecker(PermissionConfig(mode=PermissionMode.AUTO), risk_resolver=risk_resolver)


def _risk_resolver(name):
    return RISK_BY_NAME.get(name)


# ===== classify_access: file =====

@pytest.mark.parametrize("op,expected", [
    ("read", "read"),
    ("exists", "read"),
    ("list", "read"),
    ("preview", "read"),
    ("READ", "read"),          # 大小写不敏感
    ("write", "write"),
    ("delete", "write"),
    ("move", "write"),
    ("", "write"),             # 缺 operation → 保守视为写
])
def test_classify_file_operations(op, expected):
    assert _checker().classify_access("file", {"operation": op}) == expected


# ===== classify_access: shell =====

@pytest.mark.parametrize("command,expected", [
    ("ls -la", "read"),
    ("cat README.md", "read"),
    ("grep -rn foo src/", "read"),
    ("head -n 5 f.txt", "read"),
    ("pwd", "read"),
    ("  ls -la  ", "read"),    # 前后空白被 strip
    ("rm -rf /tmp/x", "write"),
    ("mv a b", "write"),
    ("python run.py", "write"),
    ("", "write"),
])
def test_classify_shell_read_prefix(command, expected):
    assert _checker().classify_access("shell", {"command": command}) == expected


# ===== classify_access: MCP 风险 =====

@pytest.mark.parametrize("name,expected", [
    ("mcp_read", "read"),
    ("mcp_write", "write"),
    ("mcp_destructive", "write"),
    ("mcp_unknown", "write"),  # 无注解/未知 → 保守按写
])
def test_classify_mcp_risk(name, expected):
    assert _checker(_risk_resolver).classify_access(name, {}) == expected


def test_classify_mcp_resolver_exception_falls_back_builtin():
    """risk_resolver 异常/返回 None(非 MCP) → 回落内置 write_tools 判定。"""
    def broken(_name):
        raise RuntimeError("boom")

    checker = _checker(broken)
    assert checker.classify_access("shell", {"command": "ls -la"}) == "read"
    assert checker.classify_access("shell", {"command": "rm -rf x"}) == "write"
    assert checker.classify_access("edit", {}) == "write"
    assert checker.classify_access("search", {}) == "read"


# ===== RBAC 统一: 种子 service / supreme 角色 =====

def test_seed_service_and_supreme_roles(rbac):
    """服务身份(service)与代授权执行者(supreme)均为 RBAC 角色。

    - service: 无工具权限(对话面 fail-closed), 但具备服务级 Web 权限键, data_scope=all;
    - supreme: 代授权执行者, 能力超集(供 actor 门禁), Web 权限为空、data_scope=self。
    """
    assert rbac.check_tool("service", "shell") is False
    assert rbac.check_tool("service", "file", is_write=True) is False
    assert rbac.get_permissions("service") == ["admin.users"]
    assert rbac.get_data_scope("service") == "all"

    assert rbac.check_tool("supreme", "shell", is_write=True) is True
    assert rbac.check_agent("supreme", "任意代理") is True
    assert rbac.get_permissions("supreme") == []
    assert rbac.get_data_scope("supreme") == "self"


# ===== RBAC check_tool: 只读限定 / 兼容 =====

def test_check_tool_readonly_entry_allows_read_denies_write(rbac):
    rbac.update_role("default", allowed_tools=["file:read"])
    assert rbac.check_tool("default", "file", is_write=False) is True
    assert rbac.check_tool("default", "file", is_write=True) is False
    # 未配 shell:read → 默认(is_write=False)也不放行
    assert rbac.check_tool("default", "shell") is False


def test_check_tool_plain_entry_read_write_both(rbac):
    """旧条目 ``file`` 不区分读写(向后兼容)。"""
    rbac.update_role("default", allowed_tools=["file"])
    assert rbac.check_tool("default", "file") is True
    assert rbac.check_tool("default", "file", is_write=True) is True


def test_check_tool_wildcard_allows_write(rbac):
    rbac.update_role("default", allowed_tools=["*"])
    assert rbac.check_tool("default", "shell", is_write=False) is True
    assert rbac.check_tool("default", "shell", is_write=True) is True


def test_check_tool_default_param_backward_compatible(rbac):
    """不传 is_write 的旧调用签名(默认 False)行为不变。"""
    rbac.update_role("default", allowed_tools=["shell"])
    assert rbac.check_tool("default", "shell") is True
    assert rbac.check_tool("default", "edit") is False


# ===== executor 集成: default + file:read =====

def _fake_agent(storage):
    return SimpleNamespace(
        rbac=RBACManager(storage),
        permission=_checker(),
        hooks=SimpleNamespace(fire=AsyncMock()),
        tracer=SimpleNamespace(start_span=lambda name: None),
        plugin_manager=None,
        sandbox=None,
        track_file_read=lambda path, content: None,
        name="test-agent",
        _hook_event=HookEvent,
    )


async def test_executor_default_role_file_readonly(monkeypatch, storage):
    import agent.executor as executor

    RBACManager(storage).update_role("default", allowed_tools=["file:read"])

    async def fake_execute(agent, name, args):
        return json.dumps({"success": True, "content": "data"}, ensure_ascii=False)

    monkeypatch.setattr(executor, "execute_tool", fake_execute)

    agent = _fake_agent(storage)
    rc = RunContext(user_id="", role="default", session=SimpleNamespace(role="default"))
    token = _current_run.set(rc)
    try:
        ok = await executor.execute_tool_safe(
            agent, "file", {"operation": "read", "path": "a.txt"})
        assert '"success": true' in ok

        denied = await executor.execute_tool_safe(
            agent, "file", {"operation": "write", "path": "a.txt"})
        assert "权限" in denied
        assert '"success": true' not in denied
    finally:
        _current_run.reset(token)


async def test_executor_classify_exception_treated_as_write(monkeypatch, storage):
    """分类异常兜底为写操作: 只读条目不放行, 普通条目仍按旧语义放行。"""
    import agent.executor as executor

    RBACManager(storage).update_role("default", allowed_tools=["file:read"])

    async def fake_execute(agent, name, args):
        return json.dumps({"success": True}, ensure_ascii=False)

    monkeypatch.setattr(executor, "execute_tool", fake_execute)

    agent = _fake_agent(storage)

    def _boom(_name, _args):
        raise RuntimeError("classify failed")

    agent.permission.classify_access = _boom

    rc = RunContext(user_id="", role="default", session=SimpleNamespace(role="default"))
    token = _current_run.set(rc)
    try:
        denied = await executor.execute_tool_safe(
            agent, "file", {"operation": "read", "path": "a.txt"})
        assert "权限" in denied
    finally:
        _current_run.reset(token)


async def test_executor_obo_intersection_requires_both(monkeypatch, storage):
    """代授权求交(on-behalf-of): actor 与 subject 都须放行, 任一方无权即拒绝。

    零号员工场景: actor=服务身份(supreme), subject=提问用户; 有效权限 = actor ∩ subject。
    个人 Agent 直连(actor_role 为空)时行为不变, 不受影响。
    """
    import agent.executor as executor

    mgr = RBACManager(storage)
    mgr.update_role("default", allowed_tools=["file:read"])  # subject: 只读
    mgr.create_role("supreme", allowed_tools=["*"])           # actor: 全放行
    mgr.create_role("restricted", allowed_tools=[])           # actor: 无任何工具

    async def fake_execute(agent, name, args):
        return json.dumps({"success": True, "content": "data"}, ensure_ascii=False)

    monkeypatch.setattr(executor, "execute_tool", fake_execute)
    agent = _fake_agent(storage)

    async def run_with(actor_role, op):
        rc = RunContext(user_id="web:2", role="default",
                        session=SimpleNamespace(role="default"),
                        actor_id="zero-employee", actor_role=actor_role)
        token = _current_run.set(rc)
        try:
            return await executor.execute_tool_safe(
                agent, "file", {"operation": op, "path": "a.txt"})
        finally:
            _current_run.reset(token)

    # actor=supreme ∩ subject(file:read): read 放行, write 拒绝(交集收窄)
    assert '"success": true' in await run_with("supreme", "read")
    assert "权限" in await run_with("supreme", "write")
    # actor=restricted 无权: 即便 subject 允许 read 也拒绝
    assert "权限" in await run_with("restricted", "read")
