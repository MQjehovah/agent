"""Pipeline DAG 单元测试"""
import sys

sys.path.insert(0, "src")

from src.team.pipeline import Pipeline, PipelineStage, RetryPolicy


def test_pipeline_creation():
    p = Pipeline(stages=[
        PipelineStage(name="research", agent="算法研究员",
                      retry_policy=RetryPolicy(max_retries=0)),
        PipelineStage(name="arch", agent="软件架构师",
                      retry_policy=RetryPolicy(max_retries=0)),
        PipelineStage(name="code", agent="代码工程师",
                      retry_policy=RetryPolicy(max_retries=3)),
    ])
    assert len(p.stages) == 3


def test_pipeline_move_next():
    p = Pipeline(stages=[
        PipelineStage(name="research", agent="算法研究员"),
        PipelineStage(name="arch", agent="软件架构师"),
    ])
    assert p.current_stage.name == "research"
    p.advance()
    assert p.current_stage.name == "arch"


def test_pipeline_complete():
    p = Pipeline(stages=[PipelineStage(name="research", agent="算法研究员")])
    p.advance()
    assert p.is_complete()


def test_pipeline_should_retry():
    stage = PipelineStage(name="code", agent="代码工程师",
                          retry_policy=RetryPolicy(max_retries=3))
    stage.attempt_count = 2
    assert stage.should_retry() is True
    stage.attempt_count = 3
    assert stage.should_retry() is False


def test_stage_record_attempt():
    stage = PipelineStage(name="code", agent="代码工程师")
    stage.record_attempt(success=True, result="All good")
    assert stage.attempt_count == 1
    assert stage.success is True
    assert stage.result == "All good"


def test_pipeline_reset_to():
    p = Pipeline(stages=[
        PipelineStage(name="research", agent="算法研究员"),
        PipelineStage(name="arch", agent="软件架构师"),
        PipelineStage(name="code", agent="代码工程师"),
    ])
    p.advance()
    p.advance()  # now at "code"
    p.reset_to("arch")
    assert p.current_stage.name == "arch"
    assert p.current_stage.attempt_count == 0
    assert p.current_stage.success is False


def test_pipeline_current_stage_none_when_done():
    p = Pipeline(stages=[PipelineStage(name="research", agent="算法研究员")])
    p.advance()
    assert p.current_stage is None
