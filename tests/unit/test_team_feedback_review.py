"""审核结论判定测试: parse_review_verdict + 编排器阶段反馈判定。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from team.feedback import parse_review_verdict  # noqa: E402


def test_parse_review_verdict_json_confirmed():
    assert parse_review_verdict('{"confirmed": true, "reason": "ok"}') is True
    assert parse_review_verdict('{"confirmed": false}') is False


def test_parse_review_verdict_conclusion_lines():
    assert parse_review_verdict("问题清单...\n审核结论：通过") is True
    assert parse_review_verdict("审核结论：不通过\n- P0 ...") is False
    # 半角冒号与省略"审核"
    assert parse_review_verdict("结论: 通过") is True
    assert parse_review_verdict("结论: 不通过") is False


def test_parse_review_verdict_ambiguous_returns_none():
    assert parse_review_verdict("这段代码看起来还行") is None
    assert parse_review_verdict("") is None
    assert parse_review_verdict(None) is None


def test_stage_needs_feedback_review_and_test_summary():
    from team.orchestrator import TeamOrchestrator

    orch = TeamOrchestrator("t", {}, {}, None, None)

    async def run():
        # 审核结论优先
        assert await orch._stage_needs_feedback("审核结论：通过") is False
        assert await orch._stage_needs_feedback("审核结论：不通过") is True
        # 测试摘要(无审核结论时)
        assert await orch._stage_needs_feedback("3 failed, 1 passed") is True
        assert await orch._stage_needs_feedback("0 failed, 5 passed") is False

    asyncio.run(run())
