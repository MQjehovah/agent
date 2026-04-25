import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("agent.team.context")


@dataclass
class MemberMessage:
    from_member: str
    to_member: str
    content: str
    timestamp: float = field(default_factory=time.time)


class TeamContext:
    def __init__(self, team_name: str, task: str, max_iterations: int = 5):
        self.team_name = team_name
        self.original_task = task
        self.node_results: dict[str, str] = {}
        self.member_outputs: dict[str, list[str]] = {}
        self.messages: list[MemberMessage] = []
        self.iteration: int = 0
        self.max_iterations = max_iterations
        self._leader_feedback: str = ""
        self.blackboard: dict[str, str] = {}

    def set_blackboard(self, key: str, value: str):
        self.blackboard[key] = value
        logger.info(f"团队黑板更新: {key} = {value}")

    def get_blackboard(self) -> str:
        if not self.blackboard:
            return ""
        lines = ["## 团队共享信息（所有成员必读）"]
        for key, value in self.blackboard.items():
            lines.append(f"- **{key}**: {value}")
        return "\n".join(lines)

    def add_node_result(self, node_id: str, assignee: str, result: str):
        self.node_results[node_id] = result
        if assignee not in self.member_outputs:
            self.member_outputs[assignee] = []
        self.member_outputs[assignee].append(result)

    def add_message(self, from_member: str, to_member: str, content: str):
        self.messages.append(MemberMessage(from_member, to_member, content))
        logger.info(f"团队消息: {from_member} -> {to_member}: {content[:80]}")

    def get_context_for_member(self, member_name: str) -> str:
        parts = []
        
        blackboard_info = self.get_blackboard()
        if blackboard_info:
            parts.append(blackboard_info)

        parts.append(f"## 团队任务\n{self.original_task}")

        upstream = self._get_relevant_results(member_name)
        if upstream:
            parts.append("## 上游产出")
            for node_id, result in upstream.items():
                parts.append(f"### {node_id}\n{result[:3000]}")

        msgs = [m for m in self.messages if m.to_member == member_name]
        if msgs:
            parts.append("## 团队消息")
            for m in msgs[-10:]:
                parts.append(f"- 来自 {m.from_member}: {m.content[:500]}")

        if self._leader_feedback:
            parts.append(f"## Leader 反馈\n{self._leader_feedback}")

        parts.append(f"\n当前执行轮次: 第 {self.iteration} 轮")
        return "\n\n".join(parts)

    def _get_relevant_results(self, member_name: str) -> dict[str, str]:
        return dict(self.node_results)

    def set_leader_feedback(self, feedback: str):
        self._leader_feedback = feedback

    def get_summary(self) -> str:
        lines = [f"# 团队执行摘要 (第 {self.iteration} 轮)\n"]
        for node_id, result in self.node_results.items():
            lines.append(f"## {node_id}\n{result[:500]}\n")
        return "\n".join(lines)

    def get_member_results(self, member_name: str) -> list[str]:
        return self.member_outputs.get(member_name, [])
