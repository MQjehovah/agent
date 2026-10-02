"""P1：按 agent 的能力作用域（tools/disallowedTools/mcpServers/skills/permissionMode）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.capabilities import CORE_TOOLS, Capabilities  # noqa: E402

# ── 解析 ─────────────────────────────────────────

def test_from_frontmatter_parsing():
    caps = Capabilities.from_frontmatter({
        "tools": "file, grep , edit",
        "disallowedTools": ["shell"],
        "mcpServers": ["gitlab", "jira"],
        "skills": ["s1", "s2"],
        "permissionMode": "smart",
    })
    assert caps.tools == ["file", "grep", "edit"]
    assert caps.disallowed_tools == ["shell"]
    assert caps.mcp_servers == ["gitlab", "jira"]
    assert caps.skills == ["s1", "s2"]
    assert caps.permission_mode == "smart"


def test_tool_denylist_alias_and_empty_mcp():
    caps = Capabilities.from_frontmatter({"tool_denylist": ["memory"], "mcpServers": []})
    assert caps.disallowed_tools == ["memory"]
    assert caps.mcp_servers == []  # 显式空 = 无 MCP


def test_mcp_absent_means_inherit():
    caps = Capabilities.from_frontmatter({"tools": ["file"]})
    assert caps.mcp_servers is None


def test_empty_capabilities():
    assert Capabilities.from_frontmatter(None).is_empty is True
    assert Capabilities.from_frontmatter({}).is_empty is True
    assert Capabilities.from_frontmatter({"tools": ["file"]}).is_empty is False


# ── 工具判定 ─────────────────────────────────────

def test_tools_allowlist_and_core_tools():
    caps = Capabilities(tools=["file"])
    assert caps.allows_tool("file") is True
    assert caps.allows_tool("shell") is False
    # 核心工具恒定可用
    for core in CORE_TOOLS:
        assert caps.allows_tool(core) is True


def test_deny_precedence_over_allow():
    caps = Capabilities(tools=["file", "shell"], disallowed_tools=["shell"])
    assert caps.allows_tool("file") is True
    assert caps.allows_tool("shell") is False


def test_empty_tools_allows_all():
    caps = Capabilities()
    assert caps.allows_tool("anything") is True


def test_core_tool_can_be_explicitly_denied():
    caps = Capabilities(disallowed_tools=["ask_user"])
    assert caps.allows_tool("ask_user") is False


# ── MCP 判定 ─────────────────────────────────────

def test_mcp_allow_none_means_all():
    assert Capabilities().allows_mcp_server("gitlab") is True


def test_mcp_allowlist_and_empty():
    assert Capabilities(mcp_servers=["gitlab"]).allows_mcp_server("jira") is False
    assert Capabilities(mcp_servers=[]).allows_mcp_server("gitlab") is False


def test_mcp_denied_by_pattern():
    caps = Capabilities(disallowed_tools=["mcp__gitlab"])
    assert caps.allows_mcp_server("gitlab") is False
    assert caps.allows_mcp_server("jira") is True


# ── 技能判定 ─────────────────────────────────────

def test_skill_allowlist():
    caps = Capabilities(skills=["s1"])
    assert caps.allows_skill("s1") is True
    assert caps.allows_skill("s2") is False
    assert Capabilities().allows_skill("any") is True


def test_override_with_member_wins():
    team = Capabilities(tools=["file"], disallowed_tools=["shell"])
    member = Capabilities(tools=["grep"])
    assert team.override_with(member) is member
    # 成员为空 → 继承团队
    assert team.override_with(Capabilities()) is team


# ── 执行层判定（executor） ────────────────────────

class _MCP:
    def has_tool(self, name):
        return name.startswith("mcp_")

    def tool_server(self, name):
        return "gitlab"


class _Agent:
    def __init__(self, caps, mcp=None):
        self.capabilities = caps
        self.mcp = mcp
        self.name = "t"


def test_executor_capability_allows_builtin():
    from agent.executor import _agent_capability_allows
    assert _agent_capability_allows(_Agent(Capabilities()), "shell") is True
    assert _agent_capability_allows(_Agent(Capabilities(tools=["file"])), "shell") is False


def test_executor_capability_allows_mcp_by_server():
    from agent.executor import _agent_capability_allows
    agent = _Agent(Capabilities(mcp_servers=["jira"]), mcp=_MCP())
    assert _agent_capability_allows(agent, "mcp_tool") is False  # server=gitlab not allowed
    agent2 = _Agent(Capabilities(mcp_servers=["gitlab"]), mcp=_MCP())
    assert _agent_capability_allows(agent2, "mcp_tool") is True


# ── 技能管理器作用域 ─────────────────────────────

def _make_skill_dir(tmp_path, names):
    for n in names:
        d = tmp_path / n
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: {n}\n---\nbody", encoding="utf-8")


def test_skill_manager_agent_scope(tmp_path, monkeypatch):
    from skills import SkillManager
    _make_skill_dir(tmp_path, ["s1", "s2"])
    sm = SkillManager(str(tmp_path))
    monkeypatch.setattr(SkillManager, "_current_user_access", staticmethod(lambda: ("", "")))
    assert {s.name for s in sm.visible_skills()} == {"s1", "s2"}
    sm.set_agent_scope(["s1"])
    assert {s.name for s in sm.visible_skills()} == {"s1"}


# ── Agent 从 PROMPT.md 加载 ──────────────────────

def test_agent_loads_capabilities_from_prompt(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "PROMPT.md").write_text(
        "---\nname: t\ntools: file, grep\ndisallowedTools: [shell]\n"
        "mcpServers: [gitlab]\nskills: [s1]\npermissionMode: plan\n---\nbody",
        encoding="utf-8",
    )
    from agent.core import Agent
    agent = Agent(workspace=str(tmp_path), client=None, config_dir=str(cfg))
    agent._load_system_prompt()
    agent._load_capabilities()
    assert agent.capabilities.tools == ["file", "grep"]
    assert agent.capabilities.disallowed_tools == ["shell"]
    assert agent.capabilities.mcp_servers == ["gitlab"]
    assert agent.capabilities.skills == ["s1"]
    assert agent._permission_config.mode.value == "plan"


def test_team_member_capability_inherit_and_override(tmp_path):
    base = tmp_path / "agents"
    team = base / "T"
    (team / "agents" / "M1").mkdir(parents=True)
    (team / "agents" / "M2").mkdir(parents=True)
    (team / "TEAM.md").write_text(
        "---\nname: T\ndescription: d\nmembers:\n  - name: M1\n  - name: M2\n"
        "tools: [file]\n---\nteam body", encoding="utf-8")
    (team / "PROMPT.md").write_text("---\nname: T\ndescription: d\n---\nleader", encoding="utf-8")
    (team / "agents" / "M1" / "PROMPT.md").write_text(
        "---\nname: M1\ndescription: d\n---\nno caps", encoding="utf-8")
    (team / "agents" / "M2" / "PROMPT.md").write_text(
        "---\nname: M2\ndescription: d\ntools: [grep]\n---\noverride", encoding="utf-8")

    from agent.subagent import SubagentManager
    mgr = SubagentManager(str(base))

    inherit = mgr._effective_member_capabilities("T", "M1")
    assert inherit.tools == ["file"]  # M1 无配置 → 继承团队
    override = mgr._effective_member_capabilities("T", "M2")
    assert override.tools == ["grep"]  # M2 整份覆盖


def test_agent_explicit_capabilities_override_file(tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "PROMPT.md").write_text("---\nname: t\ntools: file\n---\nbody", encoding="utf-8")
    from agent.core import Agent
    agent = Agent(workspace=str(tmp_path), client=None, config_dir=str(cfg),
                  capabilities=Capabilities(tools=["grep"]))
    agent._load_system_prompt()
    agent._load_capabilities()
    assert agent.capabilities.tools == ["grep"]
