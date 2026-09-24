"""SKILL.md 部门/角色可见性测试。

覆盖:
- frontmatter 解析容错: departments/roles 缺省/空列表=不限, 非列表/全非法项=fail-closed;
- 过滤语义: 两维度都非空须同时命中(AND), 单维非空不限制另一维, 用户值为空不命中;
- <available_skills> 清单按当前 run 用户过滤; skill 工具执行前二次校验(防绕过);
- 数据通路: Agent.run 设置/继承 user_department, MessageRouter.route 透传部门。
"""
import json
import logging
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest

from agent.core import Agent, RunContext, _current_run, current_run
from channels.router import MessageRouter
from skills import SkillManager


def _add_skill(skills_dir: str, name: str, front_extra: str = "",
               body: str = "工作流: {user_input}") -> str:
    """创建最小技能目录(name 唯一), front_extra 为附加 frontmatter 行。"""
    skill_dir = os.path.join(skills_dir, name)
    os.makedirs(skill_dir, exist_ok=True)
    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {name} 测试技能\n"
        f"{front_extra}"
        "---\n\n"
        f"# {name}\n\n{body}\n"
    )
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)
    return skill_dir


@contextmanager
def _as_user(department: str = "", role: str = "default", user_role: str | None = None):
    """在指定用户身份(进程内 run 上下文)下执行断言。

    user_role 缺省取 role(对应渠道显式传入 role 的场景); 传 "" 表示渠道未提供显式角色。
    """
    explicit_role = role if user_role is None else user_role
    token = _current_run.set(RunContext(
        user_department=department, role=role, user_role=explicit_role))
    try:
        yield
    finally:
        _current_run.reset(token)


def _skills_dir(tmp_path) -> str:
    d = tmp_path / "skills"
    d.mkdir()
    return str(d)


def _desc(manager: SkillManager) -> str:
    """skill 工具描述(其内嵌 <available_skills> 清单)。"""
    return manager.get_tool_definitions()[0]["function"]["description"]


# ===== 1. 无限制技能 =====

def test_unrestricted_skill_visible_for_all_users(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "open-skill")
    mgr = SkillManager(d)
    for dept, role in (("", "default"), ("信息部", "default"), ("研发部", "admin")):
        with _as_user(dept, role):
            assert "open-skill" in _desc(mgr)


async def test_unrestricted_skill_executable_for_all_users(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "open-skill")
    mgr = SkillManager(d)
    with _as_user("", "default"):
        out = await mgr.execute_tool("skill", {"name": "open-skill", "user_input": "上线"})
        assert "已激活技能: open-skill" in out
    with _as_user("信息部", "admin"):
        out = await mgr.execute_tool("skill", {"name": "open-skill"})
        assert "已激活技能: open-skill" in out


# ===== 2. departments 维度 =====

def test_department_restricted_visible_only_to_matching_department(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "it-skill", "departments: [信息部]\n")
    mgr = SkillManager(d)
    with _as_user("信息部", "default"):
        assert "it-skill" in _desc(mgr)
    with _as_user("研发部", "default"):
        assert "it-skill" not in _desc(mgr)
    with _as_user("", "admin"):
        assert "it-skill" not in _desc(mgr)  # 用户部门为空不命中(即使 admin)


async def test_department_restricted_execution_denied_for_other_or_empty_dept(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "it-skill", "departments: [信息部]\n")
    mgr = SkillManager(d)
    for dept in ("研发部", ""):
        with _as_user(dept, "default"):
            out = await mgr.execute_tool("skill", {"name": "it-skill"})
        data = json.loads(out)
        assert "error" in data
        assert "it-skill" not in data.get("available_skills", [])  # 不泄漏受限技能名
        assert "it-skill" not in mgr._active_skills               # 未激活
    with _as_user("信息部", "default"):
        out = await mgr.execute_tool("skill", {"name": "it-skill"})
    assert "已激活技能: it-skill" in out


# ===== 3. roles 维度 =====

def test_role_restricted_visible_to_admin_only(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "admin-skill", "roles: [admin]\n")
    mgr = SkillManager(d)
    with _as_user("", "admin"):
        assert "admin-skill" in _desc(mgr)
    with _as_user("", "default"):
        assert "admin-skill" not in _desc(mgr)


async def test_role_restricted_executable_by_admin(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "admin-skill", "roles: [admin]\n")
    mgr = SkillManager(d)
    with _as_user("", "admin"):
        out = await mgr.execute_tool("skill", {"name": "admin-skill"})
        assert "已激活技能: admin-skill" in out
    with _as_user("", "default"):
        out = await mgr.execute_tool("skill", {"name": "admin-skill"})
        assert "error" in json.loads(out)


def test_role_requires_explicit_channel_role_fail_closed(tmp_path):
    """渠道未提供显式角色(user_role 空, role 仅回落哨兵 default)时 restricted 技能不可见。"""
    d = _skills_dir(tmp_path)
    _add_skill(d, "default-role", "roles: [default]\n")
    mgr = SkillManager(d)
    with _as_user("", "default", user_role=""):
        assert "default-role" not in _desc(mgr)
    with _as_user("", "default"):
        assert "default-role" in _desc(mgr)


# ===== 4. 两维 AND / 单维不限制另一维 =====

@pytest.mark.parametrize("dept,role,expected", [
    ("信息部", "admin", True),
    ("信息部", "default", False),   # 角色未命中
    ("研发部", "admin", False),     # 部门未命中
    ("", "admin", False),           # 部门为空不命中
    ("信息部", "", False),          # 角色为空不命中
])
def test_both_dimensions_require_and(tmp_path, dept, role, expected):
    d = _skills_dir(tmp_path)
    _add_skill(d, "both", "departments: [信息部]\nroles: [admin]\n")
    mgr = SkillManager(d)
    assert mgr.skills["both"].is_available_to(dept, role) is expected


def test_single_dimension_does_not_restrict_the_other(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "dept-only", "departments: [信息部]\n")
    _add_skill(d, "role-only", "roles: [admin]\n")
    mgr = SkillManager(d)
    dept_only = mgr.skills["dept-only"]
    assert dept_only.is_available_to("信息部", "default")
    assert dept_only.is_available_to("信息部", "")       # roles 维度不限
    assert not dept_only.is_available_to("研发部", "admin")
    role_only = mgr.skills["role-only"]
    assert role_only.is_available_to("", "admin")        # departments 维度不限
    assert role_only.is_available_to("研发部", "admin")
    assert not role_only.is_available_to("研发部", "default")


# ===== 5. frontmatter 解析容错(明确语义并锁定) =====

def test_empty_list_means_unrestricted(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "empty-deps", "departments: []\nroles: []\n")
    s = SkillManager(d).skills["empty-deps"]
    assert s.departments == [] and s.roles == []
    assert s.access_invalid is False
    assert s.is_available_to("", "default")


def test_null_means_unrestricted(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "null-deps", "departments: null\nroles:\n")
    s = SkillManager(d).skills["null-deps"]
    assert s.is_available_to("", "default")


def test_non_list_type_fail_closed_with_warning(tmp_path, caplog):
    d = _skills_dir(tmp_path)
    _add_skill(d, "bad-roles", "roles: admin\n")
    with caplog.at_level(logging.WARNING, logger="agent.skills.skill"):
        mgr = SkillManager(d)
    s = mgr.skills["bad-roles"]
    assert s.access_invalid is True
    assert s.is_available_to("", "admin") is False        # fail-closed: 即使 admin 也不可见
    assert any("fail-closed" in r.getMessage() for r in caplog.records)
    with _as_user("", "admin"):
        assert "bad-roles" not in _desc(mgr)


def test_all_invalid_items_fail_closed(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "bad-items", "departments: [123, '']\n")
    s = SkillManager(d).skills["bad-items"]
    assert s.access_invalid is True
    assert s.is_available_to("信息部", "admin") is False


def test_mixed_valid_and_invalid_items_keep_valid_ones(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "mixed", "departments: [信息部, 123]\n")
    s = SkillManager(d).skills["mixed"]
    assert s.departments == ["信息部"]
    assert s.access_invalid is False
    assert s.is_available_to("信息部", "default")
    assert not s.is_available_to("123", "default")        # 非字符串项被忽略, 不构成限制


# ===== 6. 清单过滤 / run 之外 / 全量目录保留 =====

def test_available_skills_only_lists_visible(tmp_path):
    d = _skills_dir(tmp_path)
    _add_skill(d, "open-skill")
    _add_skill(d, "it-skill", "departments: [信息部]\n")
    mgr = SkillManager(d)
    with _as_user("信息部", "default"):
        desc = _desc(mgr)
        assert "open-skill" in desc and "it-skill" in desc
    with _as_user("研发部", "default"):
        desc = _desc(mgr)
        assert "open-skill" in desc and "it-skill" not in desc


def test_restricted_skill_invisible_outside_run(tmp_path):
    """run 之外(空身份)受限技能不可见, 无限制技能照常可见。"""
    d = _skills_dir(tmp_path)
    _add_skill(d, "open-skill")
    _add_skill(d, "it-skill", "departments: [信息部]\n")
    desc = _desc(SkillManager(d))
    assert "open-skill" in desc and "it-skill" not in desc


def test_list_skills_remains_full_catalog(tmp_path):
    """list_skills 保持全量目录(团队技能合并/日志等非 LLM 场景使用)。"""
    d = _skills_dir(tmp_path)
    _add_skill(d, "it-skill", "departments: [信息部]\n")
    assert "it-skill" in SkillManager(d).list_skills()


# ===== 7. 数据通路: Agent.run / MessageRouter =====

async def test_agent_run_sets_and_inherits_user_identity(tmp_path, monkeypatch):
    from agent import runner

    seen = {}

    async def fake_dispatch(agent, task, session_id, user_id, user_name, inherited):
        rc = current_run()
        seen[task] = (rc.user_department, rc.user_role)
        if task == "outer":
            child = Agent(workspace=str(tmp_path), client=MagicMock(), parent_agent=agent)
            await child.run("inner")
        return SimpleNamespace(result="ok")

    monkeypatch.setattr(runner, "dispatch", fake_dispatch)
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    # 渠道显式传 role(权限值)时同时作为技能过滤角色; 子代理/嵌套 run 继承部门与角色
    await agent.run("outer", user_department="信息部", role="admin")
    assert seen["outer"] == ("信息部", "admin")
    assert seen["inner"] == ("信息部", "admin")
    # 未显式传角色: user_role 保持空(role 回落 default 哨兵), 不参与技能过滤
    await agent.run("no-identity")
    assert seen["no-identity"] == ("", "")


async def test_router_passes_user_department_and_role(tmp_path):
    class _FakeAgent:
        client = SimpleNamespace(model="test")

        async def run(self, content, **kwargs):
            return kwargs

    router = MessageRouter(_FakeAgent())
    out = await router.route("hi", channel="cli",
                             user_department="信息部", user_role="admin")
    assert out["user_department"] == "信息部"
    assert out["user_role"] == "admin"
    out = await router.route("hi", channel="cli")
    assert out["user_department"] == ""   # 未提供身份: 受限技能不可见(fail-closed)
    assert out["user_role"] == ""
