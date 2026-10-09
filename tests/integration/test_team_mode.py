import os
import sys

# src/ 直接加入 path（与仓库其它测试一致；模块间为顶层导入）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.subagent import SubagentManager  # noqa: E402

TEAM_NAME = "Coding(开发)"
# 精简流水线：只保留规划/开发/审核三个常驻节点（其余角色目录保留但不参与调度）
EXPECTED_MEMBERS = {
    "软件架构师",
    "代码工程师",
    "测试工程师",
}


def _agents_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "config", "agents")


# ── 1. 真实团队可被识别 ──

def test_team_detected_in_config():
    """真实 config/agents 应包含 Coding(开发) 及其全部成员。"""
    mgr = SubagentManager(_agents_dir())
    teams = mgr.scan_teams()
    assert TEAM_NAME in teams
    assert set(teams[TEAM_NAME]) == EXPECTED_MEMBERS


def test_team_registered_as_template():
    mgr = SubagentManager(_agents_dir())
    assert TEAM_NAME in mgr.templates
    assert mgr.templates[TEAM_NAME].get("is_team") is True


# ── 2. 成员模板 frontmatter 有效 ──

def test_all_member_templates_have_valid_frontmatter():
    """每个成员 PROMPT.md 都应含 name/description/workspace。"""
    mgr = SubagentManager(_agents_dir())
    members = mgr.scan_teams()[TEAM_NAME]
    for member in members:
        template = mgr.get_team_member_template(TEAM_NAME, member)
        assert template is not None, f"Member {member} returned None"
        assert template.get("name"), f"Member {member} missing name"
        assert "description" in template, f"Member {member} missing description"
        assert template.get("workspace"), f"Member {member} missing workspace"
