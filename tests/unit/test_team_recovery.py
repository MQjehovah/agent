"""团队阶段失败恢复：失败自动重试（退避），超限标记 failed。"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from team.dag import DAGNode, ExecutionDAG  # noqa: E402
from team.orchestrator import TeamOrchestrator  # noqa: E402


class _Ctx:
    def __init__(self):
        self.stage_status: dict[str, str] = {}
        self.blackboard: dict[str, str] = {}

    def set_stage_status(self, stage, status):
        self.stage_status[stage] = status

    def set_blackboard(self, key, value):
        self.blackboard[key] = value


def _orch(max_retries: int) -> TeamOrchestrator:
    o = TeamOrchestrator.__new__(TeamOrchestrator)
    o.team_name = "t"
    o.dag = ExecutionDAG()
    o.dag.add_node(DAGNode(id="a", task="task", assignee="role", max_retries=max_retries))
    o.context = _Ctx()
    o._max_stage_retries = max_retries
    o._retry_backoff = 0.0
    o._completed_stages = set()
    return o


def test_stage_failure_recovery_resets_pending():
    o = _orch(2)
    node = o.dag.nodes["a"]
    o.dag.mark_running("a")  # attempts=1
    asyncio.run(o._handle_stage_failure(node, "boom"))
    assert node.status == "pending", node.status
    assert o.context.stage_status["a"] == "retrying"
    assert "a_retry" in o.context.blackboard


def test_stage_failure_retries_then_fails():
    o = _orch(2)
    node = o.dag.nodes["a"]
    for i in range(3):  # attempts=1,2 重试；attempts=3 超限失败
        o.dag.mark_running("a")
        asyncio.run(o._handle_stage_failure(node, f"boom{i}"))
    assert node.status == "failed", node.status
    assert o.context.stage_status["a"] == "failed"
    assert "a_error" in o.context.blackboard


def test_stage_failure_no_retry_when_disabled():
    o = _orch(0)
    node = o.dag.nodes["a"]
    o.dag.mark_running("a")
    asyncio.run(o._handle_stage_failure(node, "boom"))
    assert node.status == "failed"
