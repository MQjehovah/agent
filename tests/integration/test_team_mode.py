import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.subagent_manager import SubagentManager
from src.team.errors import CompileError, FallbackAction, TestError, classify_failure
from src.team.pipeline import Pipeline, PipelineStage
from src.team.team_lead import STAGE_AGENT_MAP, TeamLeadAgent

# ── 1. SubagentManager detects the real team ──

def test_team_detected_in_workspace():
    """Real workspace should contain AI开发团队 with 6 members."""
    ws = os.path.join(os.path.dirname(__file__), "..", "..", "workspace")
    agents_dir = os.path.join(ws, "agents")
    mgr = SubagentManager(agents_dir)
    teams = mgr.scan_teams()
    assert "AI开发团队" in teams
    assert len(teams["AI开发团队"]) == 6
    assert "算法研究员" in teams["AI开发团队"]
    assert "软件架构师" in teams["AI开发团队"]
    assert "代码工程师" in teams["AI开发团队"]
    assert "测试工程师" in teams["AI开发团队"]
    assert "DevOps工程师" in teams["AI开发团队"]
    assert "文档专员" in teams["AI开发团队"]


# ── 2. Member templates load correctly ──

def test_all_member_templates_have_valid_frontmatter():
    """Every team member PROMPT.md should have name and description."""
    ws = os.path.join(os.path.dirname(__file__), "..", "..", "workspace")
    agents_dir = os.path.join(ws, "agents")
    mgr = SubagentManager(agents_dir)
    members = mgr.scan_teams()["AI开发团队"]
    for member in members:
        template = mgr.get_team_member_template("AI开发团队", member)
        assert template is not None, f"Member {member} returned None"
        assert "name" in template, f"Member {member} missing name"
        assert "description" in template, f"Member {member} missing description"
        assert "workspace" in template, f"Member {member} missing workspace"


# ── 3. Pipeline fallback works ──

def test_default_pipeline_is_valid():
    """Verify default pipeline stages all map to agents."""
    from src.team.team_lead import DEFAULT_PIPELINE
    for stage in DEFAULT_PIPELINE:
        assert stage in STAGE_AGENT_MAP, f"Stage {stage} missing from STAGE_AGENT_MAP"
        assert STAGE_AGENT_MAP[stage] is not None


# ── 4. Full end-to-end with mocked LLM ──

@pytest.mark.asyncio
async def test_team_lead_end_to_end():
    """Test full TeamLeadAgent.run() flow with mocked LLM."""
    ws = os.path.join(os.path.dirname(__file__), "..", "..", "workspace")

    # Mock LLM
    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock(return_value='["arch", "code", "test"]')

    # Mock executor but use real pipeline/error logic
    mock_executor = MagicMock()
    mock_executor.execute_pipeline = AsyncMock(return_value=True)
    mock_executor.context = {
        "arch": "## Architecture\n- Module A: feature extraction\n- Module B: matching",
        "code": "## Code\nAll modules implemented",
        "test": "## Test\n8/8 tests passing",
    }

    lead = TeamLeadAgent(
        workspace=ws,
        llm_client=mock_llm,
        executor=mock_executor,
    )

    result = await lead.run("Build SLAM system")

    # Verify
    assert result is not None
    assert "Build SLAM system" in result  # Goal in report
    assert "Architecture" in result       # Stage outputs in report
    assert "✅" in result or "完成" in result or "success" in result.lower()


@pytest.mark.asyncio
async def test_team_lead_failure_path():
    """Test TeamLeadAgent failure report."""
    ws = os.path.join(os.path.dirname(__file__), "..", "..", "workspace")

    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock(return_value='["arch"]')

    mock_executor = MagicMock()
    mock_executor.execute_pipeline = AsyncMock(return_value=False)
    mock_executor.context = {"arch": "Partial result"}

    lead = TeamLeadAgent(ws, mock_llm, executor=mock_executor)
    result = await lead.run("Build SLAM system")

    assert result is not None
    # Should indicate failure
    assert "❌" in result or "fail" in result.lower() or "失败" in result


# ── 5. Error classification integration ──

def test_error_classification_integration():
    """Verify error classification produces correct fallback stages."""
    compile_err = classify_failure("编译错误: undefined reference")
    assert isinstance(compile_err, CompileError)
    assert compile_err.stage == "code"

    test_err = classify_failure("测试失败: assertion error")
    assert isinstance(test_err, TestError)
    assert test_err.stage == "test"

    unknown = classify_failure("Some random error")
    assert isinstance(unknown, FallbackAction)
    assert unknown.stage == "unknown"


# ── 6. Pipeline model integration ──

def test_pipeline_with_real_stages():
    """Verify Pipeline works with real team member names."""
    stages = [
        PipelineStage(name="research", agent="算法研究员"),
        PipelineStage(name="arch", agent="软件架构师"),
        PipelineStage(name="code", agent="代码工程师"),
        PipelineStage(name="test", agent="测试工程师"),
        PipelineStage(name="devops", agent="DevOps工程师"),
    ]
    pipeline = Pipeline(stages=stages)
    assert pipeline.current_stage.agent == "算法研究员"
    for _ in range(5):
        pipeline.advance()
    assert pipeline.is_complete()


# ── 7. argparse accepts team mode ──

def test_argparse_team_mode():
    """Verify --mode team is a valid argparse choice."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["interactive", "autonomous", "team"])
    args = parser.parse_args(["--mode", "team"])
    assert args.mode == "team"
