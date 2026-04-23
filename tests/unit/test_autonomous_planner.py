import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from autonomous.goal import Goal
from autonomous.planner import Planner


@pytest.mark.asyncio
async def test_plan_simple_goal():
    client = MagicMock()
    client.chat = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content=json.dumps(
                            {
                                "steps": [
                                    {"task": "获取设备列表", "requires_confirmation": False},
                                    {"task": "逐一检查设备状态", "requires_confirmation": False},
                                ]
                            }
                        )
                    )
                )
            ]
        )
    )

    planner = Planner(client=client)
    goal = Goal(title="设备巡检", description="巡检所有设备的运行状态")
    plan = await planner.plan(goal)

    assert len(plan.steps) == 2
    assert plan.steps[0].task_description == "获取设备列表"
    assert plan.goal_id == goal.id


@pytest.mark.asyncio
async def test_replan_with_feedback():
    client = MagicMock()
    client.chat = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content=json.dumps(
                            {
                                "steps": [
                                    {"task": "重新获取设备列表并重试", "requires_confirmation": False},
                                ]
                            }
                        )
                    )
                )
            ]
        )
    )

    planner = Planner(client=client)
    goal = Goal(title="设备巡检", description="巡检")
    completed_step = MagicMock(status="completed", task_description="获取设备列表")
    failed_step = MagicMock(
        status="failed", task_description="检查设备状态", result="连接超时"
    )

    plan = await planner.replan(
        goal, "检查设备状态失败: 连接超时", [completed_step], [failed_step]
    )
    assert len(plan.steps) >= 1
