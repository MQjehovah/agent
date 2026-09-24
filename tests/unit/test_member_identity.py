"""团队/子代理成员 run 身份透传测试(不得注入 cli:admin 假身份)。

覆盖:
- TeamOrchestrator._run_stage 成员 run 传父 run 真实 user_id/user_name,
  会话提示词出现真实用户、不出现"管理员";
- AgentPool.execute / map 两条路径同样透传真实身份。
"""
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.core import Agent, RunContext, _current_run, current_run


def _child_agent(tmp_path) -> Agent:
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    # 避免 cleanup 误删仓库根目录下的相对 "tmp"
    agent.temp_dir = str(tmp_path / "_child_tmp")
    return agent


class _FakeSubagentManager:
    _team_name = ""

    def __init__(self, agent):
        self._agent = agent

    async def _create_team_subagent(self, team_name, role, **kwargs):
        return self._agent


def _capture_dispatch(seen: dict):
    async def fake_dispatch(agent, task, session_id, user_id, user_name, inherited):
        rc = current_run()
        seen["user_id"] = rc.user_id
        seen["user_name"] = rc.user_name
        seen["dynamic"] = rc.system_dynamic
        return SimpleNamespace(result="ok", status="completed")
    return fake_dispatch


async def test_team_stage_member_prompt_uses_real_identity(tmp_path, monkeypatch):
    """团队 stage 成员 run: 真实身份进会话提示词, 不再有"管理员"假身份。"""
    from agent import runner
    from team.context import TeamContext
    from team.orchestrator import TeamOrchestrator

    child = _child_agent(tmp_path)
    seen = {}
    monkeypatch.setattr(runner, "dispatch", _capture_dispatch(seen))

    orch = TeamOrchestrator(
        team_name="测试团队",
        team_config={"name": "测试团队", "workspace": str(tmp_path),
                     "team_body": "", "leader": "", "pipeline_mode": "feedback"},
        members={"工程师": {}},
        subagent_manager=_FakeSubagentManager(child),
        llm_client=None,
    )
    orch.workspace = str(tmp_path)
    orch.context = TeamContext("测试团队", "写一个函数")

    token = _current_run.set(RunContext(
        user_id="web:3", user_name="张明",
        user_department="数字中台部", role="editor", user_role="editor"))
    try:
        result = await orch._run_stage("工程师", "implementation", None)
    finally:
        _current_run.reset(token)

    assert result == "ok"
    assert seen["user_id"] == "web:3"
    assert seen["user_name"] == "张明"
    assert "张明" in seen["dynamic"] and "数字中台部" in seen["dynamic"]
    assert "管理员" not in seen["dynamic"]


async def test_pool_execute_uses_real_identity(tmp_path, monkeypatch):
    from agent import runner
    from agent.pool import AgentPool

    child = _child_agent(tmp_path)
    seen = {}
    monkeypatch.setattr(runner, "dispatch", _capture_dispatch(seen))
    pool = AgentPool(subagent_manager=_FakeSubagentManager(child), max_size=2)

    token = _current_run.set(RunContext(
        user_id="web:7", user_name="李雷",
        user_department="信息部", role="default", user_role="default"))
    try:
        text = await pool.execute("任务", role="工程师", team_name="测试团队")
    finally:
        _current_run.reset(token)

    assert text == "ok"
    assert seen["user_id"] == "web:7"
    assert seen["user_name"] == "李雷"
    assert "李雷" in seen["dynamic"] and "信息部" in seen["dynamic"]
    assert "管理员" not in seen["dynamic"]


async def test_pool_map_uses_real_identity(tmp_path, monkeypatch):
    from agent import runner
    from agent.pool import AgentPool

    child = _child_agent(tmp_path)
    seen = {}
    monkeypatch.setattr(runner, "dispatch", _capture_dispatch(seen))
    pool = AgentPool(subagent_manager=_FakeSubagentManager(child), max_size=2)

    token = _current_run.set(RunContext(
        user_id="dingtalk:9", user_name="韩梅梅",
        user_department="售后部", role="default", user_role="default"))
    try:
        results = await pool.map([{"task": "子任务", "role": "工程师",
                                   "team_name": "测试团队", "session_id": "s1"}])
    finally:
        _current_run.reset(token)

    assert results == [("工程师", "ok")]
    assert seen["user_id"] == "dingtalk:9"
    assert seen["user_name"] == "韩梅梅"
    assert "韩梅梅" in seen["dynamic"]
    assert "管理员" not in seen["dynamic"]


async def test_pool_execute_without_run_uses_empty_identity(tmp_path, monkeypatch):
    """run 之外拿不到身份: 传空, 不注入假身份。"""
    from agent import runner
    from agent.pool import AgentPool

    child = _child_agent(tmp_path)
    seen = {}
    monkeypatch.setattr(runner, "dispatch", _capture_dispatch(seen))
    pool = AgentPool(subagent_manager=_FakeSubagentManager(child), max_size=2)

    await pool.execute("任务", role="工程师", team_name="测试团队")

    assert seen["user_id"] == ""
    assert seen["user_name"] == ""
    assert "管理员" not in seen["dynamic"]
