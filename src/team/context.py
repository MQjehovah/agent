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


@dataclass
class FeedbackLoop:
    """开发↔测试反馈循环状态"""
    iteration: int = 0
    max_iterations: int = 3
    developer_role: str = "代码工程师"
    tester_role: str = "测试工程师"
    test_results: list[dict] = field(default_factory=list)
    fix_history: list[dict] = field(default_factory=list)

    @property
    def should_continue(self) -> bool:
        return self.iteration < self.max_iterations

    @property
    def all_passed(self) -> bool:
        if not self.test_results:
            return False
        return all(r.get("passed", False) for r in self.test_results)

    def to_context_string(self) -> str:
        parts = [f"## 开发↔测试循环 (第 {self.iteration}/{self.max_iterations} 轮)"]
        if self.test_results:
            parts.append("### 测试结果")
            for i, r in enumerate(self.test_results):
                status = "✅ 通过" if r.get("passed") else "❌ 失败"
                parts.append(f"- 测试{i + 1}: {status} — {r.get('name', '未命名')}")
                if not r.get("passed") and r.get("details"):
                    parts.append(f"  详情: {r['details'][:500]}")
        if self.fix_history:
            parts.append("### 修复历史")
            for h in self.fix_history:
                parts.append(f"- 第{h.get('iteration', '?')}轮修复: {h.get('summary', '')[:300]}")
        return "\n".join(parts)


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
        self.feedback_loop = FeedbackLoop()
        self.stage_status: dict[str, str] = {}
        self.started_at = time.time()
        self.token_usage: dict[str, int] = {}

    def set_blackboard(self, key: str, value: str):
        self.blackboard[key] = value
        preview = (value[:120] + "...") if len(value) > 120 else value
        logger.info(f"团队黑板更新: {key} = {preview}")

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

        blackboard_info = self._get_blackboard_compact()
        if blackboard_info:
            parts.append(blackboard_info)

        parts.append(f"## 团队任务\n{self.original_task}")

        # 上游阶段产出（避免下游重读文件）
        upstream_results = [(nid, r) for nid, r in self.node_results.items() if r]
        if upstream_results:
            lines = ["## 上游阶段产出", "以下是你之前各阶段的产出摘要，避免重复劳动：", ""]
            for nid, result in upstream_results:
                truncated = result[:1500]
                if len(result) > 1500:
                    truncated += "\n...（截断）"
                lines.append(f"### {nid}\n{truncated}\n")
            parts.append("\n".join(lines))

        artifact_index = self._get_artifact_index()
        if artifact_index:
            parts.append(artifact_index)

        msgs = [m for m in self.messages if m.to_member == member_name]
        if msgs:
            parts.append("## 团队消息")
            for m in msgs[-10:]:
                parts.append(f"- 来自 {m.from_member}: {m.content[:500]}")

        if self._leader_feedback:
            parts.append(f"## Leader 反馈\n{self._leader_feedback}")

        if self.feedback_loop.iteration > 0:
            parts.append(self.feedback_loop.to_context_string())

        parts.append(f"\n当前执行轮次: 第 {self.iteration} 轮")
        return "\n\n".join(parts)

    def _get_relevant_results(self, member_name: str) -> dict[str, str]:
        return dict(self.node_results)

    def _get_blackboard_compact(self) -> str:
        """精简版黑板：只输出索引信息，不输出大段内容"""
        if not self.blackboard:
            return ""
        skip_keys = {k for k in self.blackboard if k.endswith("_result")}
        lines = ["## 团队共享信息"]
        for key, value in self.blackboard.items():
            if key in skip_keys:
                continue
            if len(value) > 200:
                lines.append(f"- **{key}**: {value[:200]}...")
            else:
                lines.append(f"- **{key}**: {value}")
        return "\n".join(lines)

    def _get_artifact_index(self) -> str:
        """生成产出物索引：只列出路径和摘要，不塞入内容"""
        artifact_keys = [k for k in self.blackboard if k.endswith("_output")]
        if not artifact_keys:
            return ""

        lines = ["## 上游产出物索引", "请使用 file_operation 工具按需读取以下文件：", ""]
        for key in artifact_keys:
            path = self.blackboard[key]
            stage_name = key.replace("_output", "")
            error_key = f"{stage_name}_error"
            status = "失败" if error_key in self.blackboard else "完成"
            lines.append(f"- **{stage_name}** ({status}): `{path}`")

        return "\n".join(lines)

    def set_leader_feedback(self, feedback: str):
        self._leader_feedback = feedback

    def get_summary(self) -> str:
        lines = [f"# 团队执行摘要 (第 {self.iteration} 轮)\n"]
        for node_id, result in self.node_results.items():
            lines.append(f"## {node_id}\n{result[:200]}...\n")
        artifact_lines = [f"- {k}: {v}" for k, v in self.blackboard.items() if k.endswith("_output")]
        if artifact_lines:
            lines.append("## 产出物\n" + "\n".join(artifact_lines))
        return "\n".join(lines)

    def get_member_results(self, member_name: str) -> list[str]:
        return self.member_outputs.get(member_name, [])

    def set_stage_status(self, stage: str, status: str):
        self.stage_status[stage] = status

    def get_stage_summary(self) -> str:
        if not self.stage_status:
            return "无阶段执行记录"
        lines = []
        for stage, status in self.stage_status.items():
            icon = {"completed": "✓", "failed": "✗", "running": "⟳"}.get(status, "·")
            lines.append(f"  {icon} {stage}: {status}")
        return "\n".join(lines)
