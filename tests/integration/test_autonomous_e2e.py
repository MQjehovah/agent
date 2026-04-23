import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from autonomous.eventbus import Event, EventBus
from autonomous.executor import Executor
from autonomous.goal import GoalManager
from autonomous.loop import AutonomousLoop
from autonomous.perceiver import Perceiver
from autonomous.planner import Planner
from autonomous.reporter import Reporter
from autonomous.verifier import Verifier


@pytest.mark.asyncio
async def test_e2e_user_message_to_completed_goal(tmp_path):
    bus = EventBus()
    db_path = str(tmp_path / "test_auto.db")
    goal_mgr = GoalManager(db_path)

    perceiver_response = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "is_goal": True,
                            "title": "设备巡检",
                            "description": "检查所有设备状态",
                        }
                    )
                )
            )
        ]
    )
    planner_response = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "steps": [
                                {"task": "检查所有设备状态", "requires_confirmation": False}
                            ]
                        }
                    )
                )
            )
        ]
    )
    verifier_response = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "passed": True,
                            "confidence": 0.95,
                            "summary": "设备巡检完成",
                            "feedback": "",
                        }
                    )
                )
            )
        ]
    )

    agent = MagicMock()
    agent.client = MagicMock()
    agent.client.chat = AsyncMock(
        side_effect=[perceiver_response, planner_response, verifier_response]
    )
    agent.run = AsyncMock(
        return_value=MagicMock(status="completed", result="10台设备全部正常")
    )
    agent.memory = None
    agent.workspace = str(tmp_path)

    perceiver = Perceiver(event_bus=bus, agent=agent)
    planner = Planner(client=agent.client)
    executor = Executor(agent=agent, reporter=Reporter())
    verifier = Verifier(client=agent.client)
    reporter = Reporter()

    shutdown = asyncio.Event()
    loop = AutonomousLoop(
        event_bus=bus,
        agent=agent,
        goal_manager=goal_mgr,
        planner=planner,
        executor=executor,
        verifier=verifier,
        reporter=reporter,
        perceiver=perceiver,
        shutdown_event=shutdown,
    )

    await bus.publish(
        Event(
            type="user_message",
            source="dingtalk",
            payload={"text": "帮我做一次设备巡检", "sender_nick": "张三"},
            priority=4,
        )
    )

    result = await loop._process_next_goal(timeout=5.0)
    assert result is not None
    assert result.status == "completed"

    fetched = goal_mgr.get_goal(result.id)
    assert fetched is not None
    assert fetched.status == "completed"
