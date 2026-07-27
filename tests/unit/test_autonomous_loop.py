import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from unittest.mock import AsyncMock, MagicMock

import pytest

from autonomous.eventbus import Event, EventBus
from autonomous.goal import Goal
from autonomous.loop import AutonomousLoop


@pytest.mark.asyncio
async def test_process_single_goal_success():
    bus = EventBus()
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(status="completed", result="ok"))
    agent.client = MagicMock()
    agent.memory = None

    goal_manager = MagicMock()
    goal = Goal(title="测试", description="测试任务", source="user")
    goal_manager.create_goal.return_value = goal

    planner = MagicMock()
    planner.plan = AsyncMock(
        return_value=MagicMock(
            goal_id=goal.id,
            steps=[
                MagicMock(
                    task_description="步骤1",
                    order=1,
                    status="pending",
                    requires_confirmation=False,
                    result=None,
                )
            ],
        )
    )

    executor = MagicMock()
    executor.execute_plan = AsyncMock(
        return_value=[MagicMock(status="completed", task_description="步骤1")]
    )

    verifier = MagicMock()
    verifier.verify = AsyncMock(
        return_value=MagicMock(
            passed=True, confidence=0.9, summary="成功", feedback=""
        )
    )

    reporter = MagicMock()
    reporter.report_success = AsyncMock()
    reporter.report_failure = AsyncMock()

    perceiver = MagicMock()
    perceiver.resolve_goal_from_event = AsyncMock(
        return_value={"is_goal": True, "title": "测试", "description": "测试任务"}
    )

    loop = AutonomousLoop(
        event_bus=bus,
        agent=agent,
        goal_manager=goal_manager,
        planner=planner,
        executor=executor,
        verifier=verifier,
        reporter=reporter,
        perceiver=perceiver,
    )

    await bus.publish(
        Event(type="user_message", source="test", payload={"text": "测试"})
    )
    result = await loop._process_next_goal(timeout=2.0)

    assert result is not None
    assert result.status == "completed"
    reporter.report_success.assert_called_once()


@pytest.mark.asyncio
async def test_process_goal_with_retry():
    bus = EventBus()
    agent = MagicMock()
    agent.run = AsyncMock(return_value=MagicMock(status="failed", result="error"))
    agent.client = MagicMock()
    agent.memory = None

    goal_manager = MagicMock()
    goal = Goal(title="测试", description="测试", source="user", max_retries=1)
    goal_manager.create_goal.return_value = goal
    goal_manager.increment_retry = MagicMock()

    planner = MagicMock()
    planner.plan = AsyncMock(return_value=MagicMock(goal_id=goal.id, steps=[]))
    planner.replan = AsyncMock(return_value=MagicMock(goal_id=goal.id, steps=[]))

    executor = MagicMock()
    executor.execute_plan = AsyncMock(return_value=[])

    verifier = MagicMock()
    verifier.verify = AsyncMock(
        return_value=MagicMock(
            passed=False, confidence=0.5, summary="失败", feedback="重试"
        )
    )

    reporter = MagicMock()
    reporter.report_failure = AsyncMock()

    perceiver = MagicMock()
    perceiver.resolve_goal_from_event = AsyncMock(
        return_value={"is_goal": True, "title": "测试", "description": "测试"}
    )

    loop = AutonomousLoop(
        event_bus=bus,
        agent=agent,
        goal_manager=goal_manager,
        planner=planner,
        executor=executor,
        verifier=verifier,
        reporter=reporter,
        perceiver=perceiver,
    )

    await bus.publish(
        Event(type="user_message", source="test", payload={"text": "测试"})
    )
    result = await loop._process_next_goal(timeout=2.0)

    assert result is not None
    assert result.status == "failed"
