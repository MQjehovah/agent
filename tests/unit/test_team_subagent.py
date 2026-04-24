"""团队子代理单元测试"""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

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
    创建团队目录结构:
    agents/{team_name}/TEAM.md
    agents/{team_name}/members/{member}/PROMPT.md
    """
    team_dir = os.path.join(base, "agents", team_name)
    os.makedirs(team_dir, exist_ok=True)

    with open(os.path.join(team_dir, "TEAM.md"), "w", encoding="utf-8") as f:
        f.write(f"# {team_name}\n")

    members_dir = os.path.join(team_dir, "members")
    os.makedirs(members_dir, exist_ok=True)

    for member_name, prompt_content in members.items():
        member_dir = os.path.join(members_dir, member_name)
        os.makedirs(member_dir, exist_ok=True)
        with open(os.path.join(member_dir, "PROMPT.md"), "w", encoding="utf-8") as f:
            f.write(prompt_content)


class TestTeamSubagent:
    """团队子代理功能测试"""

    def test_scan_teams_returns_team(self, tmp_path):
        """Verify scan_teams identifies AI开发团队 with its members"""
        _create_team_dir(tmp_path, "AI开发团队", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
            "后端开发": "---\nname: 后端开发\ndescription: 后端开发工程师\n---\n你负责后端开发。",
        })

        from subagent_manager import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        teams = manager.scan_teams()
        assert "AI开发团队" in teams
        assert "前端开发" in teams["AI开发团队"]
        assert "后端开发" in teams["AI开发团队"]

    def test_scan_teams_skips_non_team(self, tmp_path):
        """Verify regular agent dirs (设备运维) don't appear as teams"""
        _create_agent_dir(tmp_path, "设备运维",
                          "---\nname: 设备运维\ndescription: 设备运维专家\n---\n你负责设备运维。")
        _create_team_dir(tmp_path, "AI开发团队", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
        })

        from subagent_manager import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        teams = manager.scan_teams()
        assert "设备运维" not in teams
        assert "AI开发团队" in teams

    def test_scan_teams_skips_agents_outside_members(self, tmp_path):
        """Verify a dir with TEAM.md but no members/ is not treated as team"""
        team_dir = os.path.join(tmp_path, "agents", "不完整团队")
        os.makedirs(team_dir, exist_ok=True)
        with open(os.path.join(team_dir, "TEAM.md"), "w", encoding="utf-8") as f:
            f.write("# 不完整团队\n")

        from subagent_manager import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        teams = manager.scan_teams()
        assert "不完整团队" not in teams

    def test_get_team_member_template_loads_prompt(self, tmp_path):
        """Verify get_team_member_template returns frontmatter correctly"""
        _create_team_dir(tmp_path, "AI开发团队", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
        })

        from subagent_manager import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        tmpl = manager.get_team_member_template("AI开发团队", "前端开发")
        assert tmpl is not None
        assert tmpl["name"] == "前端开发"
        assert tmpl["description"] == "前端开发工程师"
        assert tmpl["workspace"].endswith(os.path.join("AI开发团队", "members", "前端开发"))

    def test_get_team_member_template_nonexistent(self, tmp_path):
        """Verify get_team_member_template returns None for missing member"""
        _create_team_dir(tmp_path, "AI开发团队", {})

        from subagent_manager import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        tmpl = manager.get_team_member_template("AI开发团队", "不存在成员")
        assert tmpl is None

    def test_existing_agents_still_work(self, tmp_path):
        """Verify _load_all still loads non-team agents"""
        _create_agent_dir(tmp_path, "设备运维",
                          "---\nname: 设备运维\ndescription: 设备运维专家\n---\n你负责设备运维。")
        _create_agent_dir(tmp_path, "售后客服",
                          "---\nname: 售后客服\ndescription: 售后客服\n---\n你负责售后客服。")
        _create_team_dir(tmp_path, "AI开发团队", {
            "前端开发": "---\nname: 前端开发\ndescription: 前端开发工程师\n---\n你负责前端开发。",
        })

        from subagent_manager import SubagentManager
        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        # 验证普通代理仍然被加载
        assert "设备运维" in manager.templates
        assert "售后客服" in manager.templates

        # 验证团队目录不出现在模板中
        assert "AI开发团队" not in manager.templates
        assert "前端开发" not in manager.templates

    @pytest.mark.asyncio
    async def test_run_team_agent(self, tmp_path):
        """Verify run_team_agent delegates to _create_team_subagent and runs the agent"""
        _create_team_dir(tmp_path, "AI开发团队", {
            "算法研究员": "---\nname: 算法研究员\ndescription: AI算法研究员\n---\n你负责算法研究。",
        })

        from agent import Agent, AgentResult
        from subagent_manager import SubagentManager

        manager = SubagentManager(os.path.join(tmp_path, "agents"))

        mock_agent = AsyncMock(spec=Agent)
        mock_agent.cleanup = AsyncMock()
        mock_agent.run = AsyncMock(return_value=AgentResult(agent_id="test", status="success", result="研究成果"))

        with patch.object(manager, "_create_team_subagent", new=AsyncMock(return_value=mock_agent)) as mock_create:
            result = await manager.run_team_agent("AI开发团队", "算法研究员", "研究新算法")

        mock_create.assert_called_once_with(
            "AI开发团队", "算法研究员",
            client=None, parent_agent=None,
        )
        mock_agent.run.assert_called_once_with("研究新算法")
        assert result == "研究成果"

    @pytest.mark.asyncio
    async def test_run_team_agent_does_not_leak_to_templates(self, tmp_path):
        """Verify team member templates don't leak into self.templates"""
        _create_agent_dir(tmp_path, "设备运维",
                          "---\nname: 设备运维\ndescription: 设备运维专家\n---\n你负责设备运维。")
        _create_team_dir(tmp_path, "AI开发团队", {
            "算法研究员": "---\nname: 算法研究员\ndescription: AI算法研究员\n---\n你负责算法研究。",
        })

        from agent import Agent, AgentResult
        from subagent_manager import SubagentManager

        manager = SubagentManager(os.path.join(tmp_path, "agents"))
        original_templates = dict(manager.templates)

        mock_agent = AsyncMock(spec=Agent)
        mock_agent.cleanup = AsyncMock()
        mock_agent.run = AsyncMock(return_value=AgentResult(agent_id="test", status="success", result="研究成果"))

        with patch.object(manager, "_create_team_subagent", new=AsyncMock(return_value=mock_agent)):
            await manager.run_team_agent("AI开发团队", "算法研究员", "研究新算法")

        assert manager.templates == original_templates
        assert "AI开发团队/算法研究员" not in manager.templates
