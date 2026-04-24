import sys

sys.path.insert(0, "src")

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.team.team_lead import TeamLeadAgent


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat = AsyncMock()
    return llm


@pytest.fixture
def mock_executor():
    exc = MagicMock()
    exc.execute_pipeline = AsyncMock(return_value=True)
    exc.context = {"arch": "Arch done", "code": "Code done"}
    return exc


@pytest.mark.asyncio
async def test_plan_pipeline_parses_json(mock_llm):
    mock_llm.chat.return_value = '["arch", "code", "test", "devops"]'
    lead = TeamLeadAgent("/tmp/test", mock_llm, None, None)
    pipeline = await lead._plan_pipeline("Build a SLAM system")
    assert pipeline is not None
    assert len(pipeline.stages) == 4
    assert pipeline.stages[0].name == "arch"
    assert pipeline.stages[0].agent == "软件架构师"


@pytest.mark.asyncio
async def test_plan_pipeline_fallback_on_bad_json(mock_llm):
    mock_llm.chat.return_value = "not json at all"
    lead = TeamLeadAgent("/tmp/test", mock_llm, None, None)
    pipeline = await lead._plan_pipeline("Build a SLAM system")
    assert pipeline is not None
    # Should fallback to default pipeline
    assert len(pipeline.stages) >= 3


@pytest.mark.asyncio
async def test_plan_pipeline_empty_response(mock_llm):
    mock_llm.chat.return_value = ""
    lead = TeamLeadAgent("/tmp/test", mock_llm, None, None)
    pipeline = await lead._plan_pipeline("Build a SLAM system")
    assert pipeline is not None
    assert len(pipeline.stages) >= 3


@pytest.mark.asyncio
async def test_full_run_success(mock_llm, mock_executor):
    mock_llm.chat.return_value = '["arch", "test"]'
    lead = TeamLeadAgent("/tmp/test", mock_llm, mock_executor, None)
    result = await lead.run("Build a SLAM system")
    assert result is not None
    assert "Build a SLAM system" in result
    mock_executor.execute_pipeline.assert_called_once()


@pytest.mark.asyncio
async def test_full_run_failure(mock_llm, mock_executor):
    mock_llm.chat.return_value = '["arch"]'
    mock_executor.execute_pipeline = AsyncMock(return_value=False)
    mock_executor.context = {"arch": "Partial result"}
    lead = TeamLeadAgent("/tmp/test", mock_llm, mock_executor, None)
    result = await lead.run("Build a SLAM system")
    assert result is not None
    assert "partial" in result.lower() or "失败" in result
