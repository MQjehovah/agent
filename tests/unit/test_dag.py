import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import pytest
from team.dag import DAGNode, ExecutionDAG


@pytest.mark.asyncio
async def test_sequential_execution():
    dag = ExecutionDAG()
    dag.add_node(DAGNode(id="a", task="task_a", assignee="agent_a"))
    dag.add_node(DAGNode(id="b", task="task_b", assignee="agent_b", dependencies=["a"]))
    order = []

    async def exec(node):
        order.append(node.id)
        return f"result_{node.id}"

    assert await dag.execute(exec)
    assert order == ["a", "b"]
    assert dag.is_complete()


@pytest.mark.asyncio
async def test_parallel_execution():
    dag = ExecutionDAG()
    dag.add_node(DAGNode(id="a", task="task_a", assignee="agent_a"))
    dag.add_node(DAGNode(id="b", task="task_b", assignee="agent_b"))
    order = []

    async def exec(node):
        order.append(node.id)
        await asyncio.sleep(0.01)
        return f"result_{node.id}"

    assert await dag.execute(exec)
    assert set(order) == {"a", "b"}


@pytest.mark.asyncio
async def test_diamond_dag():
    dag = ExecutionDAG()
    dag.add_node(DAGNode(id="a", task="t", assignee="x"))
    dag.add_node(DAGNode(id="b", task="t", assignee="x", dependencies=["a"]))
    dag.add_node(DAGNode(id="c", task="t", assignee="x", dependencies=["a"]))
    dag.add_node(DAGNode(id="d", task="t", assignee="x", dependencies=["b", "c"]))
    order = []

    async def exec(node):
        order.append(node.id)
        return "ok"

    assert await dag.execute(exec)
    assert order[0] == "a"
    assert order[-1] == "d"
    assert set(order[1:3]) == {"b", "c"}


@pytest.mark.asyncio
async def test_failure_marks_failed():
    dag = ExecutionDAG()
    dag.add_node(DAGNode(id="a", task="t", assignee="x"))

    async def exec(node):
        raise RuntimeError("boom")

    assert not await dag.execute(exec)
    assert dag.nodes["a"].status == "failed"
    assert dag.nodes["a"].error == "boom"


@pytest.mark.asyncio
async def test_dependency_chain_blocks():
    dag = ExecutionDAG()
    dag.add_node(DAGNode(id="a", task="t", assignee="x"))
    dag.add_node(DAGNode(id="b", task="t", assignee="x", dependencies=["a"]))

    async def exec(node):
        if node.id == "a":
            raise RuntimeError("fail")
        return "ok"

    result = await dag.execute(exec)
    assert not result
    assert dag.nodes["a"].status == "failed"
    assert dag.nodes["b"].status == "failed"


@pytest.mark.asyncio
async def test_get_ready_nodes():
    dag = ExecutionDAG()
    dag.add_node(DAGNode(id="a", task="t", assignee="x"))
    dag.add_node(DAGNode(id="b", task="t", assignee="x", dependencies=["a"]))
    dag.add_node(DAGNode(id="c", task="t", assignee="x", dependencies=["a"]))

    ready = dag.get_ready_nodes()
    assert len(ready) == 1
    assert ready[0].id == "a"

    dag.mark_running("a")
    dag.mark_completed("a", "done")

    ready = dag.get_ready_nodes()
    assert len(ready) == 2
    assert {n.id for n in ready} == {"b", "c"}


@pytest.mark.asyncio
async def test_to_dict():
    dag = ExecutionDAG()
    dag.add_node(DAGNode(id="a", task="do thing", assignee="x", dependencies=[]))
    dag.mark_running("a")
    dag.mark_completed("a", "result text")

    d = dag.to_dict()
    assert len(d) == 1
    assert d[0]["id"] == "a"
    assert d[0]["status"] == "completed"
