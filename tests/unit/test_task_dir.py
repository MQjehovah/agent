"""测试：任务级过程目录 task_dir（顶层 run 建立，全局临时文件隔离）。

验证 _init_task_dir 建立隔离目录（.agent/{时间戳}_{任务摘要}/artifacts/）、
特殊字符清理、不同任务不同目录、RunContext.task_dir 默认值。
run() 的建立/继承逻辑由 test_concurrency 覆盖（不破坏并发隔离）。
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from agent import Agent, RunContext  # noqa: E402


def test_init_task_dir_creates_isolated_dir(tmp_path):
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    tdir = agent._init_task_dir("查上半年产品经营情况")
    assert tdir.startswith(str(tmp_path))
    assert ".agent" in tdir
    assert os.path.isdir(tdir)  # makedirs 生效
    assert "查上半年产品经营情况" in tdir  # slug 含任务摘要


def test_init_task_dir_sanitizes_special_chars(tmp_path):
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    tdir = agent._init_task_dir("修复 bug: 登录/注册!!!")
    assert os.path.isdir(tdir)
    # 特殊字符被替换为下划线
    assert ":" not in tdir and "!" not in tdir


def test_different_tasks_different_dirs(tmp_path):
    agent = Agent(workspace=str(tmp_path), client=MagicMock())
    d1 = agent._init_task_dir("任务A_经营分析")
    d2 = agent._init_task_dir("任务B_代码审查")
    assert d1 != d2  # 不同任务不同目录


def test_run_context_task_dir_default():
    rc = RunContext()
    assert rc.task_dir == ""


def test_env_context_advertises_task_dir(tmp_path):
    """_get_env_context 在 run 上下文内应告知临时文件目录。"""
    from agent import RunContext, _current_run, current_run
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
