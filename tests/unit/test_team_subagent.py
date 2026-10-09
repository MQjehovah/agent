"""团队子代理单元测试"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))


def _create_agent_dir(base, name, prompt_content):
    """创建普通子代理目录"""
    agent_dir = os.path.join(base, "agents", name)
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "PROMPT.md"), "w", encoding="utf-8") as f:
        f.write(prompt_content)
    return agent_dir


def _create_team_dir(base, team_name, members):
    """
    创建团队目录结构（现行契约）:
    agents/{team_name}/PROMPT.md（团队人设，团队也作为 is_team 模板出现）
    agents/{team_name}/TEAM.md
    agents/{team_name}/agents/{member}/PROMPT.md
    """
    team_dir = os.path.join(base, "agents", team_name)
    os.makedirs(team_dir, exist_ok=True)

    with open(os.path.join(team_dir, "PROMPT.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {team_name}\ndescription: 测试团队\n---\n团队人设。")

    with open(os.path.join(team_dir, "TEAM.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {team_name}\ndescription: 测试团队\n---\n# {team_name}\n")

    members_dir = os.path.join(team_dir, "agents")
    os.makedirs(members_dir, exist_ok=True)

    for member_name, prompt_content in members.items():
        member_dir = os.path.join(members_dir, member_name)
        os.makedirs(member_dir, exist_ok=True)
        with open(os.path.join(member_dir, "PROMPT.md"), "w", encoding="utf-8") as f:
            f.write(prompt_content)


class TestTeamSubagent:
    """团队子代理功能测试"""

    def test_scan_teams_returns_team(self, tmp_path):
        """Verify scan_teams identifies Coding(开发) with its members"""
        _create_team_dir(tmp_path, "Coding(开发)", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
            "后端开发": "---\nname: 后端开发\ndescription: 后端开发工程师\n---\n你负责后端开发。",
        })

        from agent.subagent import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        teams = manager.scan_teams()
        assert "Coding(开发)" in teams
        assert "前端开发" in teams["Coding(开发)"]
        assert "后端开发" in teams["Coding(开发)"]

    def test_scan_teams_skips_non_team(self, tmp_path):
        """Verify regular agent dirs (设备运维) don't appear as teams"""
        _create_agent_dir(tmp_path, "设备运维",
                          "---\nname: 设备运维\ndescription: 设备运维专家\n---\n你负责设备运维。")
        _create_team_dir(tmp_path, "Coding(开发)", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
        })

        from agent.subagent import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        teams = manager.scan_teams()
        assert "设备运维" not in teams
        assert "Coding(开发)" in teams

    def test_scan_teams_skips_agents_outside_members(self, tmp_path):
        """Verify a dir with TEAM.md but no members/ is not treated as team"""
        team_dir = os.path.join(tmp_path, "agents", "不完整团队")
        os.makedirs(team_dir, exist_ok=True)
        with open(os.path.join(team_dir, "TEAM.md"), "w", encoding="utf-8") as f:
            f.write("# 不完整团队\n")

        from agent.subagent import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        teams = manager.scan_teams()
        assert "不完整团队" not in teams

    def test_get_team_member_template_loads_prompt(self, tmp_path):
        """Verify get_team_member_template returns frontmatter correctly"""
        _create_team_dir(tmp_path, "Coding(开发)", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
        })

        from agent.subagent import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        tmpl = manager.get_team_member_template("Coding(开发)", "前端开发")
        assert tmpl is not None
        assert tmpl["name"] == "前端开发"
        assert tmpl["description"] == "前端开发工程师"
        assert tmpl["config_dir"].endswith(os.path.join("Coding(开发)", "agents", "前端开发"))

    def test_get_team_member_template_nonexistent(self, tmp_path):
        """Verify get_team_member_template returns None for missing member"""
        _create_team_dir(tmp_path, "Coding(开发)", {})

        from agent.subagent import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        tmpl = manager.get_team_member_template("Coding(开发)", "不存在成员")
        assert tmpl is None

    def test_existing_agents_still_work(self, tmp_path):
        """Verify _load_all still loads non-team agents"""
        _create_agent_dir(tmp_path, "设备运维",
                          "---\nname: 设备运维\ndescription: 设备运维专家\n---\n你负责设备运维。")
        _create_agent_dir(tmp_path, "售后客服",
                          "---\nname: 售后客服\ndescription: 售后客服\n---\n你负责售后客服。")
        _create_team_dir(tmp_path, "Coding(开发)", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
        })

        from agent.subagent import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        # 普通代理被加载；团队以 is_team 模板出现，但成员不进顶层模板
        assert "设备运维" in manager.templates
        assert "售后客服" in manager.templates
        assert "Coding(开发)" in manager.templates
        assert manager.templates["Coding(开发)"].get("is_team") is True
        assert "前端开发" not in manager.templates
