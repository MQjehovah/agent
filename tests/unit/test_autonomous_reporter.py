import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from unittest.mock import MagicMock

import pytest

from autonomous.goal import Goal, PlanStep
from autonomous.reporter import DingTalkReporter, Reporter
from autonomous.verifier import VerificationResult


@pytest.mark.asyncio
async def test_log_reporter_progress():
    reporter = Reporter()
    step = PlanStep(
        plan_id="p1", task_description="测试步骤", status="completed", result="ok", order=1
    )
    await reporter.report_progress(step, MagicMock(result="ok"))


@pytest.mark.asyncio
async def test_log_reporter_success():
    reporter = Reporter()
    goal = Goal(title="测试目标", description="测试")
    await reporter.report_success(goal)


@pytest.mark.asyncio
async def test_log_reporter_failure():
    reporter = Reporter()
    goal = Goal(title="测试目标", description="测试")
    verification = VerificationResult(
        passed=False, confidence=0.8, summary="失败", feedback="需要重试"
    )
    await reporter.report_failure(goal, verification)


@pytest.mark.asyncio
async def test_dingtalk_reporter_calls_send():
    dt_plugin = MagicMock()
    dt_plugin.sessions = {"test_session": MagicMock(conversation_id="conv123")}
    reporter = DingTalkReporter(dingtalk_plugin=dt_plugin, default_session_id="test_session")

    step = PlanStep(
        plan_id="p1", task_description="测试", status="completed", result="ok", order=1
    )
    await reporter.report_progress(step, MagicMock(result="ok"))
