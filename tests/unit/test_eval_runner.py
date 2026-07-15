"""P4a 测试：评估回归框架（mock 冒烟，不依赖真实 LLM）。

验证 EvalRunner 的硬断言判定、LLM-as-judge 解析、passed 聚合逻辑。
真实评估（连真实 LLM）的用例应单独标记 @pytest.mark.eval 并默认跳过。
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from eval_runner import EvalRunner, GoldenCase  # noqa: E402


def _mock_agent_factory(output: str):
    def factory():
        agent = MagicMock()
        result = MagicMock()
        result.result = output
        agent.run = AsyncMock(return_value=result)
        return agent
    return factory


@pytest.mark.asyncio
async def test_run_case_hard_assertion_passes():
    runner = EvalRunner(agent_factory=_mock_agent_factory("计算结果是 5"))
    case = GoldenCase(id="math", task="计算2+3", criteria=["结果正确"], must_contain=["5"])
    r = await runner.run_case(case)
    assert r.hard_pass is True
    assert r.judge is None
    assert r.passed is True


@pytest.mark.asyncio
async def test_run_case_hard_assertion_fails():
    runner = EvalRunner(agent_factory=_mock_agent_factory("我不知道"))
    case = GoldenCase(id="math", task="计算2+3", criteria=[], must_contain=["5"])
    r = await runner.run_case(case)
    assert r.hard_pass is False
    assert r.passed is False


@pytest.mark.asyncio
async def test_run_case_with_judge_high_score():
    judge = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = '{"score": 5, "reason": "正确"}'
    judge.chat = AsyncMock(return_value=resp)
    runner = EvalRunner(agent_factory=_mock_agent_factory("5"), judge_client=judge)
    case = GoldenCase(id="math", task="计算2+3", criteria=["结果正确"], must_contain=["5"])
    r = await runner.run_case(case)
    assert r.judge == {"score": 5, "reason": "正确"}
    assert r.passed is True


@pytest.mark.asyncio
async def test_judge_low_score_fails_even_if_hard_pass():
    judge = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = '{"score": 2, "reason": "错误"}'
    judge.chat = AsyncMock(return_value=resp)
    runner = EvalRunner(agent_factory=_mock_agent_factory("5"), judge_client=judge)
    case = GoldenCase(id="x", task="t", criteria=["c"], must_contain=[])
    r = await runner.run_case(case)
    assert r.hard_pass is True
    assert r.passed is False  # judge 低分


@pytest.mark.asyncio
async def test_run_cases_batch():
    runner = EvalRunner(agent_factory=_mock_agent_factory("ok"))
    cases = [GoldenCase(id="a", task="t1", criteria=[]), GoldenCase(id="b", task="t2", criteria=[])]
    results = await runner.run_cases(cases)
    assert len(results) == 2
    assert {r.case_id for r in results} == {"a", "b"}
