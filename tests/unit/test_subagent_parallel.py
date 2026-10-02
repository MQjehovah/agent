"""并行 subagent 硬化：创建去重、实例运行串行、全局并发信号量。"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import agent.core as core  # noqa: E402
from agent.subagent import SubagentInstance, SubagentManager  # noqa: E402


@pytest.mark.asyncio
async def test_instance_run_lock_serializes():
    inst = SubagentInstance(agent=object(), template="t", session_id="s")
    order: list[tuple[str, str]] = []

    async def work(tag):
        async with inst.run_lock:
            order.append(("start", tag))
            await asyncio.sleep(0.05)
            order.append(("end", tag))

    await asyncio.gather(work("a"), work("b"))
    # 无交叉：每个 start 后紧跟同 tag 的 end
    assert order[0][0] == "start" and order[1][0] == "end"
    assert order[0][1] == order[1][1]
    assert order[2][0] == "start" and order[3][0] == "end"


def test_parallel_limit_env(tmp_path, monkeypatch):
    base = tmp_path / "agents"
    base.mkdir()

    monkeypatch.setenv("AGENT_SUBAGENT_PARALLEL", "0")
    assert SubagentManager(str(base)).run_semaphore()._value == 1  # 强制串行

    monkeypatch.setenv("AGENT_SUBAGENT_PARALLEL", "1")
    monkeypatch.setenv("AGENT_SUBAGENT_MAX_PARALLEL", "2")
    assert SubagentManager(str(base)).run_semaphore()._value == 2

    monkeypatch.delenv("AGENT_SUBAGENT_MAX_PARALLEL", raising=False)
    assert SubagentManager(str(base)).run_semaphore()._value == 4  # 默认

    monkeypatch.setenv("AGENT_SUBAGENT_MAX_PARALLEL", "0")
    assert SubagentManager(str(base)).run_semaphore() is None  # <=0 不限


@pytest.mark.asyncio
async def test_concurrent_first_call_creates_once(tmp_path, monkeypatch):
    """并发首调同 (对话, session/template) 只创建一个实例（创建锁去重）。"""
    created: list = []

    class _FakeAgent:
        def __init__(self, **kwargs):
            self.plugin_manager = None
            self.agent_id = ""
            self.name = ""
            self.system_prompt = ""
            self.system_prompt_raw = ""
            self.system_static = ""
            self.system_dynamic = ""
            created.append(self)

        async def initialize(self):
            await asyncio.sleep(0.02)

    monkeypatch.setattr(core, "Agent", _FakeAgent)

    base = tmp_path / "agents"
    base.mkdir()
    mgr = SubagentManager(str(base))

    results = await asyncio.gather(*[
        mgr.get_or_create_subagent(
            template="market_expert", name="market_expert", session_id="s1",
            system_prompt="你是专家", allow_dynamic=True,
        )
        for _ in range(5)
    ])
    instances = {id(r[0]) for r in results}
    assert len(instances) == 1        # 同一个实例
    assert len(created) == 1          # 只创建一次
    assert all(r[1] is False for r in results[1:])  # 其余均为复用


@pytest.mark.asyncio
async def test_team_run_lock_serializes(tmp_path):
    base = tmp_path / "agents"
    base.mkdir()
    mgr = SubagentManager(str(base))
    lock = mgr.team_run_lock("conv|Team")
    assert lock is mgr.team_run_lock("conv|Team")
    assert lock is not mgr.team_run_lock("conv|Other")


def test_worktree_enabled_env(monkeypatch):
    monkeypatch.delenv("AGENT_SUBAGENT_WORKTREE", raising=False)
    assert SubagentManager._worktree_enabled() is False
    monkeypatch.setenv("AGENT_SUBAGENT_WORKTREE", "1")
    assert SubagentManager._worktree_enabled() is True


@pytest.mark.asyncio
async def test_maybe_worktree_git(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("git"):
        pytest.skip("git not installed")
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    base = tmp_path / "agents"
    base.mkdir()
    mgr = SubagentManager(str(base))
    wm, wt, ws = await mgr._maybe_worktree(str(tmp_path), "coder")
    assert wm is not None and wt and ws == wt and os.path.isdir(wt)
    await wm.cleanup_worktree_by_path(wt)
    assert not os.path.isdir(wt)


@pytest.mark.asyncio
async def test_maybe_worktree_non_git(tmp_path):
    base = tmp_path / "agents"
    base.mkdir()
    mgr = SubagentManager(str(base))
    wm, wt, ws = await mgr._maybe_worktree(str(tmp_path), "coder")
    assert wm is None and wt == "" and ws == str(tmp_path)
