"""测试：任务级过程目录 task_dir（顶层 run 建立，临时/报告目录隔离）。

现行语义（Agent._init_task_dir）：
- workspace/.agent/tmp/     —— 本次/临时文件（每次 run 重建）
- workspace/.agent/report/  —— 用户明确要求生成/保存报告时的输出目录
RunContext.task_dir 默认空串，run 建立后由 _get_env_context 告知。
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent.core import Agent, RunContext  # noqa: E402


def test_init_task_dir_creates_tmp_and_report_dirs(tmp_path):
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    tdir = agent._init_task_dir("查上半年产品经营情况")
    assert tdir == os.path.join(str(tmp_path), ".agent", "tmp")
    assert os.path.isdir(tdir)  # makedirs 生效
    assert os.path.isdir(os.path.join(str(tmp_path), ".agent", "report"))


def test_init_task_dir_recreates_clean_dir(tmp_path):
    """每次 run 重建：遗留临时文件被清理。"""
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    tdir = agent._init_task_dir("任务A")
    with open(os.path.join(tdir, "leftover.txt"), "w", encoding="utf-8") as f:
        f.write("stale")
    tdir2 = agent._init_task_dir("任务B")
    assert tdir2 == tdir
    assert not os.path.exists(os.path.join(tdir2, "leftover.txt"))


def test_run_context_task_dir_default():
    rc = RunContext()
    assert rc.task_dir == ""


def test_env_context_advertises_task_dir(tmp_path):
    """_get_env_context 在 run 上下文内应告知临时文件目录。"""
    from agent.core import _current_run

    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    ctx = RunContext(user_id="u1")
    ctx.task_dir = agent._init_task_dir("测试任务")
    token = _current_run.set(ctx)
    try:
        env = agent._get_env_context()
    finally:
        _current_run.reset(token)
    assert "临时文件目录" in env
    assert ctx.task_dir in env
