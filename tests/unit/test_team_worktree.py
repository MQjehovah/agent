"""团队 worktree 隔离接线测试"""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from team.orchestrator import TeamOrchestrator, _team_worktree_enabled  # noqa: E402


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t.local"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    return tmp_path


def test_worktree_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_TEAM_WORKTREE", raising=False)
    assert _team_worktree_enabled() is False


@pytest.mark.asyncio
async def test_prepare_and_cleanup_worktree(git_repo, monkeypatch):
    monkeypatch.setenv("AGENT_TEAM_WORKTREE", "1")
    orch = TeamOrchestrator(
        team_name="Coding(开发)", team_config={}, members={},
        subagent_manager=None, llm_client=None,
    )
    orch.workspace = str(git_repo)

    await orch._prepare_worktree()
    assert orch._run_worktree is not None
    wt = orch.workspace
    assert os.path.isdir(wt)
    assert os.path.isfile(os.path.join(wt, "a.txt"))

    await orch._cleanup_worktree()
    assert orch._run_worktree is None
    assert not os.path.isdir(wt)


@pytest.mark.asyncio
async def test_prepare_skips_non_git(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TEAM_WORKTREE", "1")
    orch = TeamOrchestrator(
        team_name="t", team_config={}, members={},
        subagent_manager=None, llm_client=None,
    )
    orch.workspace = str(tmp_path)
    await orch._prepare_worktree()
    assert orch._run_worktree is None
    assert orch.workspace == str(tmp_path)
