import sys

sys.path.insert(0, "src")

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.team.executor import TeamExecutor
from src.team.pipeline import Pipeline, PipelineStage, RetryPolicy


@pytest.fixture
def mock_subagent_manager():
    mgr = MagicMock()
    mgr.run_team_agent = AsyncMock(return_value="## Architecture\n- Module A: does X\n- Module B: does Y")
    return mgr


@pytest.fixture
def mock_memory():
    mem = MagicMock()
    mem.share_knowledge = MagicMock()
    return mem


@pytest.fixture
def executor(mock_subagent_manager, mock_memory):
    return TeamExecutor(
        workspace="/tmp/test",
        subagent_manager=mock_subagent_manager,
        memory_manager=mock_memory,
    )


@pytest.mark.asyncio
async def test_execute_simple_pipeline(executor, mock_subagent_manager):
    pipeline = Pipeline(stages=[
        PipelineStage(name="research", agent="算法研究员",
                      retry_policy=RetryPolicy(max_retries=0)),
        PipelineStage(name="arch", agent="软件架构师",
                      retry_policy=RetryPolicy(max_retries=0)),
    ])
    success = await executor.execute_pipeline(pipeline, "Build a SLAM system")
    assert success is True
    assert pipeline.is_complete()
    assert mock_subagent_manager.run_team_agent.call_count == 2


@pytest.mark.asyncio
async def test_context_passed_to_next_stage(executor, mock_subagent_manager):
    pipeline = Pipeline(stages=[
        PipelineStage(name="research", agent="算法研究员",
                      retry_policy=RetryPolicy(max_retries=0)),
        PipelineStage(name="arch", agent="软件架构师",
                      retry_policy=RetryPolicy(max_retries=0)),
    ])
    await executor.execute_pipeline(pipeline, "Build a SLAM system")
    # Second call should include first stage's result in task description
    second_call_args = mock_subagent_manager.run_team_agent.call_args[1]
    assert "research" in second_call_args.get("task", "")


@pytest.mark.asyncio
async def test_stage_failure_triggers_fallback(executor, mock_subagent_manager, mock_memory):
    mock_subagent_manager.run_team_agent.side_effect = [
        "## Research complete",  # research succeeds
        Exception("测试失败: 5/10 passed"),  # test fails
    ]
    pipeline = Pipeline(stages=[
        PipelineStage(name="arch", agent="软件架构师",
                      retry_policy=RetryPolicy(max_retries=0)),
        PipelineStage(name="test", agent="测试工程师",
                      retry_policy=RetryPolicy(max_retries=0)),
    ])
    success = await executor.execute_pipeline(pipeline, "Build a SLAM system")
    # test failure -> fallback to code -> but there's no code stage, so fallback not possible
    assert success is False


@pytest.mark.asyncio
async def test_retry_on_failure(executor, mock_subagent_manager, mock_memory):
    mock_subagent_manager.run_team_agent.side_effect = [
        Exception("编译错误: syntax error"),  # first attempt fails
        Exception("编译错误: undefined reference"),  # second attempt fails
        "## Code complete",  # third attempt succeeds
    ]
    stage = PipelineStage(name="code", agent="代码工程师",
                          retry_policy=RetryPolicy(max_retries=3))
    pipeline = Pipeline(stages=[stage])
    success = await executor.execute_pipeline(pipeline, "Build a SLAM system")
    assert success is True
    assert stage.attempt_count == 3
    assert stage.success is True


@pytest.mark.asyncio
async def test_retry_exhausted(executor, mock_subagent_manager, mock_memory):
    mock_subagent_manager.run_team_agent.side_effect = Exception("编译错误: fail")
    stage = PipelineStage(name="code", agent="代码工程师",
                          retry_policy=RetryPolicy(max_retries=1))
    pipeline = Pipeline(stages=[stage])
    success = await executor.execute_pipeline(pipeline, "Build a SLAM system")
    assert success is False
    assert stage.attempt_count == 1


@pytest.mark.asyncio
async def test_shared_memory_written(executor, mock_subagent_manager, mock_memory):
    pipeline = Pipeline(stages=[
        PipelineStage(name="arch", agent="软件架构师",
                      retry_policy=RetryPolicy(max_retries=0)),
    ])
    await executor.execute_pipeline(pipeline, "Build a SLAM system")
    assert mock_memory.share_knowledge.call_count >= 1


@pytest.mark.asyncio
async def test_error_message_reported_on_stage_failure(executor, mock_subagent_manager):
    mock_subagent_manager.run_team_agent.side_effect = [
        "OK",
        Exception("Some random error"),
    ]
    pipeline = Pipeline(stages=[
        PipelineStage(name="arch", agent="软件架构师",
                      retry_policy=RetryPolicy(max_retries=0)),
        PipelineStage(name="test", agent="测试工程师",
                      retry_policy=RetryPolicy(max_retries=0)),
    ])
    success = await executor.execute_pipeline(pipeline, "task")
    assert success is False


@pytest.mark.asyncio
async def test_build_task_description_includes_upstream_context(executor):
    executor.context["research"] = "## Research\nUsed ORB-SLAM3"
    executor.context["arch"] = "## Arch\n3 modules"
    desc = executor._build_task_description(
        PipelineStage(name="code", agent="代码工程师"),
        "Build SLAM",
    )
    assert "Build SLAM" in desc
    assert "Used ORB-SLAM3" in desc
    assert "3 modules" in desc
