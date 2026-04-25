import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger("agent.team.dag")


@dataclass
class DAGNode:
    id: str
    task: str
    assignee: str
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"
    result: str | None = None
    error: str | None = None
    attempts: int = 0
    max_retries: int = 2


class ExecutionDAG:
    def __init__(self):
        self.nodes: dict[str, DAGNode] = {}

    def add_node(self, node: DAGNode):
        self.nodes[node.id] = node

    def get_ready_nodes(self) -> list[DAGNode]:
        ready = []
        for node in self.nodes.values():
            if node.status != "pending":
                continue
            deps_met = all(
                self.nodes[d].status == "completed"
                for d in node.dependencies
                if d in self.nodes
            )
            deps_exist = all(d in self.nodes for d in node.dependencies)
            if deps_met and deps_exist:
                ready.append(node)
        return ready

    def mark_running(self, node_id: str):
        self.nodes[node_id].status = "running"
        self.nodes[node_id].attempts += 1

    def mark_completed(self, node_id: str, result: str):
        self.nodes[node_id].status = "completed"
        self.nodes[node_id].result = result

    def mark_failed(self, node_id: str, error: str):
        node = self.nodes[node_id]
        node.status = "failed"
        node.error = error

    def is_complete(self) -> bool:
        return all(n.status == "completed" for n in self.nodes.values())

    def has_pending_or_running(self) -> bool:
        return any(n.status in ("pending", "running") for n in self.nodes.values())

    async def execute(
        self,
        executor: Callable[[DAGNode], Awaitable[str]],
        max_parallel: int = 3,
    ) -> bool:
        sem = asyncio.Semaphore(max_parallel)
        max_total_attempts = len(self.nodes) * 3
        total_attempts = 0

        while self.has_pending_or_running():
            ready = self.get_ready_nodes()
            if not ready:
                running = [n for n in self.nodes.values() if n.status == "running"]
                if running:
                    await asyncio.sleep(0.05)
                    total_attempts += 1
                    if total_attempts > max_total_attempts:
                        logger.warning("DAG 执行超时，存在未完成的 running 节点")
                        break
                    continue
                pending = [n for n in self.nodes.values() if n.status == "pending"]
                if pending:
                    logger.warning(
                        f"存在 {len(pending)} 个 pending 节点但无可执行项，可能依赖缺失"
                    )
                    for n in pending:
                        n.status = "failed"
                        n.error = "依赖节点不存在或未完成"
                break

            async def _run_node(node: DAGNode):
                async with sem:
                    self.mark_running(node.id)
                    try:
                        result = await executor(node)
                        self.mark_completed(node.id, result)
                    except Exception as e:
                        self.mark_failed(node.id, str(e))
                        logger.warning(f"节点 {node.id} 执行失败: {e}")

            await asyncio.gather(*[_run_node(n) for n in ready])
            total_attempts += len(ready)
            if total_attempts > max_total_attempts:
                logger.warning("DAG 执行超过最大尝试次数")
                break

        return self.is_complete()

    def get_result_summary(self) -> dict[str, str]:
        return {
            n.id: n.result or n.error or ""
            for n in self.nodes.values()
        }

    def to_dict(self) -> list[dict]:
        return [
            {
                "id": n.id,
                "task": n.task,
                "assignee": n.assignee,
                "dependencies": n.dependencies,
                "status": n.status,
                "result": (n.result or "")[:200] if n.result else None,
            }
            for n in self.nodes.values()
        ]
