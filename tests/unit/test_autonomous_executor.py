import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from autonomous.executor import Executor
from autonomous.goal import Plan, PlanStep


@pytest.mark.asyncio
async def test_execute_plan_success():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(status="completed", result="设备列表: 10台"))

    reporter = MagicMock()
    reporter.report_progress = AsyncMock()
    reporter.ask_confirmation = AsyncMock(return_value=True)

    executor = Executor(agent=agent, reporter=reporter)

    plan = Plan(goal_id="g1", steps=[
        PlanStep(plan_id="p1", task_description="获取设备列表", order=1),
        PlanStep(plan_id="p1", task_description="检查状态", order=2),
    ])

    results = await executor.execute_plan(plan)
    assert len(results) == 2
    assert results[0].status == "completed"
    assert results[1].status == "completed"
    assert agent.run.call_count == 2


@pytest.mark.asyncio
async def test_execute_plan_with_confirmation():
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(status="completed", result="已删除"))

    reporter = MagicMock()
    reporter.report_progress = AsyncMock()
    reporter.ask_confirmation = AsyncMock(return_value=False)

    executor = Executor(agent=agent, reporter=reporter)

    plan = Plan(goal_id="g1", steps=[
        PlanStep(plan_id="p1", task_description="删除旧数据", requires_confirmation=True, order=1),
        PlanStep(plan_id="p1", task_description="生成报告", order=2),
    ])

    results = await executor.execute_plan(plan)
    assert len(results) == 1
    assert results[0].status == "rejected"
    assert agent.run.call_count == 1


@pytest.mark.asyncio
async def test_execute_plan_step_failure_stops():
    agent = MagicMock()
    agent.run = AsyncMock(side_effect=[
        MagicMock(status="completed", result="ok"),
        MagicMock(status="failed", result="连接超时"),
    ])

    reporter = MagicMock()
    reporter.report_progress = AsyncMock()

    executor = Executor(agent=agent, reporter=reporter)

    plan = Plan(goal_id="g1", steps=[
        PlanStep(plan_id="p1", task_description="步骤1", order=1),
        PlanStep(plan_id="p1", task_description="步骤2", order=2),
        PlanStep(plan_id="p1", task_description="步骤3", order=3),
    ])

    results = await executor.execute_plan(plan)
    assert len(results) == 2
    assert results[0].status == "completed"
    assert results[1].status == "failed"
    assert agent.run.call_count == 2
