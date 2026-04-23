import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from autonomous.goal import Goal, PlanStep
from autonomous.verifier import VerificationResult, Verifier


@pytest.mark.asyncio
async def test_verify_passed():
    client = MagicMock()
    client.chat = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content=json.dumps(
                            {
                                "passed": True,
                                "confidence": 0.95,
                                "summary": "所有设备正常",
                                "feedback": "",
                            }
                        )
                    )
                )
            ]
        )
    )

    verifier = Verifier(client=client)
    goal = Goal(title="设备巡检", description="检查所有设备状态")
    steps = [
        PlanStep(
            plan_id="p1", order=0, task_description="获取设备列表",
            status="completed", result="10台设备",
        ),
        PlanStep(
            plan_id="p1", order=1, task_description="检查状态",
            status="completed", result="全部正常",
        ),
    ]

    result = await verifier.verify(goal, steps)
    assert result.passed is True
    assert result.confidence > 0.5


@pytest.mark.asyncio
async def test_verify_failed():
    client = MagicMock()
    client.chat = AsyncMock(
        return_value=MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content=json.dumps(
                            {
                                "passed": False,
                                "confidence": 0.8,
                                "summary": "部分设备异常",
                                "feedback": "3台设备连接超时，需要重新检查",
                            }
                        )
                    )
                )
            ]
        )
    )

    verifier = Verifier(client=client)
    goal = Goal(title="设备巡检", description="检查所有设备状态")
    steps = [
        PlanStep(
            plan_id="p1", order=0, task_description="获取设备列表",
            status="completed", result="10台设备",
        ),
        PlanStep(
            plan_id="p1", order=1, task_description="检查状态",
            status="failed", result="3台连接超时",
        ),
    ]

    result = await verifier.verify(goal, steps)
    assert result.passed is False
    assert "超时" in result.feedback


def test_verification_result_dataclass():
    result = VerificationResult(passed=True, confidence=0.9, summary="ok", feedback="")
    assert result.passed is True
