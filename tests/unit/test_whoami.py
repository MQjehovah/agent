"""whoami 内置工具测试。

覆盖:
- 直接调用: 字段/工号/部门/角色/渠道/用户 ID; 缺失显示"未知";
- role 回退(user_role 空时用 role)、display_name 兜底姓名、群聊仍返回触发人;
- 非数字 uid 不查 rbac; rbac/storage 不可用回退;
- 注册: 自动发现为核心工具(渐进模式下不进远程检索、恒注入);
- run 级: 工具表含 whoami, 经 ToolRegistry 执行返回真实画像。
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.core import Agent, RunContext, _current_run
from tools import ToolRegistry
from tools.whoami import WhoamiTool


def _patch_rbac(monkeypatch, user):
    from agent import user_profile
    monkeypatch.setattr(user_profile, "resolve_rbac_user",
                        lambda uid, rbac=None: user)


def _tool_def(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {}}}


class _FakeMCP:
    def __init__(self, defs):
        self.tool_defs = list(defs)

    def set_reserved_names(self, names):
        pass

    def tool_server(self, name):
        return "srv"

    def is_tool_available(self, name):
        return True


async def _execute(ctx) -> str:
    token = _current_run.set(ctx)
    try:
        return await WhoamiTool().execute()
    finally:
        _current_run.reset(token)


# ===== 1. 直接调用: 字段与回退 =====

async def test_whoami_returns_profile_fields(monkeypatch):
    _patch_rbac(monkeypatch, {"name": "202202100024", "display_name": "张明",
                              "department": "数字中台部", "role": "editor"})
    out = await _execute(RunContext(user_id="web:3", user_name="张明",
                                    user_department="数字中台部", user_role="editor"))
    assert "姓名：张明" in out
    assert "工号：202202100024" in out
    assert "部门：数字中台部" in out
    assert "角色：editor" in out
    assert "渠道：web" in out
    assert "用户 ID：3" in out


async def test_whoami_display_name_and_role_fallback(monkeypatch):
    """姓名缺失用 rbac display_name; user_role 为空回退 role。"""
    _patch_rbac(monkeypatch, {"name": "202202100024", "display_name": "张三"})
    out = await _execute(RunContext(user_id="web:3", role="default"))
    assert "姓名：张三" in out
    assert "角色：default" in out


async def test_whoami_missing_fields_show_unknown(monkeypatch):
    _patch_rbac(monkeypatch, None)
    out = await _execute(RunContext(user_id="cli:admin", role=""))
    for field in ("姓名：未知", "工号：未知", "部门：未知", "角色：未知",
                  "钉钉 userId：未知", "用户 ID：未知"):
        assert field in out
    assert "渠道：cli" in out


async def test_whoami_includes_dingtalk_uid(monkeypatch):
    """已绑定钉钉: whoami 返回 platform_uid。"""
    from agent import user_profile

    monkeypatch.setattr(user_profile, "resolve_rbac_user",
                        lambda uid, rbac=None: {"id": 3, "name": "202202100024",
                                                "display_name": "季明清"})
    monkeypatch.setattr(user_profile, "resolve_dingtalk_uid",
                        lambda user_id, rbac=None: "1642483198771392"
                        if str(user_id) == "3" else "")
    out = await _execute(RunContext(user_id="web:3", user_name="季明清",
                                    user_department="应用软件部", user_role="admin"))
    assert "姓名：季明清" in out
    assert "工号：202202100024" in out
    assert "部门：应用软件部" in out
    assert "角色：admin" in out
    assert "钉钉 userId：1642483198771392" in out


async def test_whoami_group_context_still_returns_trigger_user(monkeypatch):
    """群聊时工具是显式调用: 仍返回触发人身份(仅身份字段)。"""
    _patch_rbac(monkeypatch, None)
    out = await _execute(RunContext(user_id="dingtalk:7", user_name="韩梅梅",
                                    user_department="售后部", user_role="default",
                                    group_context=True))
    assert "姓名：韩梅梅" in out
    assert "部门：售后部" in out
    assert "渠道：dingtalk" in out
    assert "用户 ID：7" in out


async def test_whoami_non_digit_uid_skips_rbac_lookup(monkeypatch):
    from agent import user_profile

    calls: list[str] = []
    monkeypatch.setattr(user_profile, "resolve_rbac_user",
                        lambda uid, rbac=None: calls.append(uid) or None)
    out = await _execute(RunContext(user_id="cli:admin", user_name="管理员"))
    assert calls == []                    # 非数字 uid 不查库
    assert "姓名：管理员" in out
    assert "用户 ID：未知" in out


# ===== 2. rbac 解析: 真实查库 + 不可用回退 =====

def test_resolve_user_profile_with_real_rbac(tmp_path):
    from agent.user_profile import resolve_user_profile
    from security.rbac import RBACManager
    from storage.storage import Storage

    storage = Storage(str(tmp_path))
    rbac = RBACManager(storage)
    uid = rbac.create_user(name="202202100024", department="数字中台部",
                           role="default", display_name="张明")
    rbac.bind_identity(uid, "dingtalk", "1642483198771392")
    profile = resolve_user_profile(RunContext(user_id=f"web:{uid}"), rbac=rbac)
    assert profile["uid"] == str(uid)
    assert profile["channel"] == "web"
    assert profile["name"] == "张明"
    assert profile["employee_id"] == "202202100024"
    assert profile["department"] == "数字中台部"      # ctx 空 → rbac 兜底
    assert profile["dingtalk"] == "1642483198771392"


def test_resolve_user_profile_dingtalk_empty_when_unbound(tmp_path):
    from agent.user_profile import resolve_user_profile
    from security.rbac import RBACManager
    from storage.storage import Storage

    rbac = RBACManager(Storage(str(tmp_path)))
    uid = rbac.create_user(name="202202100024", department="数字中台部")
    profile = resolve_user_profile(RunContext(user_id=f"web:{uid}"), rbac=rbac)
    assert profile["dingtalk"] == ""


def test_resolve_rbac_user_returns_none_when_storage_unavailable(monkeypatch):
    import storage.storage as storage_mod
    from agent.user_profile import resolve_rbac_user
    monkeypatch.setattr(storage_mod, "get_storage", lambda: None)
    assert resolve_rbac_user("3") is None


# ===== 3. 注册与注入 =====

async def test_whoami_registered_and_core_injected_in_progressive_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_SEARCH", "always")
    registry = ToolRegistry()
    registry.auto_discover()
    tool = registry.get_tool("whoami")
    assert tool is not None
    assert tool.parameters["required"] == []

    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    agent.tool_registry = registry
    agent.mcp = _FakeMCP([_tool_def(f"remote_{i}") for i in range(41)])
    token = _current_run.set(RunContext(conversation_id="web:3:c1",
                                        user_id="web:3", user_name="张明"))
    try:
        names = [d["function"]["name"] for d in agent.tool_defs]
        remote_names = [e.name for e in agent._remote_tool_entries()]
    finally:
        _current_run.reset(token)
    assert "whoami" in names            # 渐进模式下核心工具恒注入
    assert "whoami" not in remote_names  # 不进远程检索


async def test_run_tool_table_contains_whoami_and_tool_returns_profile(tmp_path, monkeypatch):
    from agent import runner, user_profile

    monkeypatch.setattr(user_profile, "resolve_rbac_user",
                        lambda uid, rbac=None: None)
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    registry = ToolRegistry()
    registry.auto_discover()
    agent.tool_registry = registry

    seen = {}

    async def fake_dispatch(agent_, task, session_id, user_id, user_name, inherited):
        seen["names"] = [d["function"]["name"] for d in agent_.tool_defs]
        seen["out"] = await agent_.tool_registry.execute("whoami", {})
        return SimpleNamespace(result="ok", status="completed")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    await agent.run("我是谁？", user_id="web:3", user_name="张明",
                    user_department="数字中台部", user_role="editor")

    assert "whoami" in seen["names"]
    assert "姓名：张明" in seen["out"]
    assert "部门：数字中台部" in seen["out"]
