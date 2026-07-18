"""
团队上下文 — 管理团队成员间的信息传递

v2.0 改进：
- 最小上下文：每个角色只接收完成任务所需的最小信息
- 角色依赖图：定义每个角色需要哪些上游阶段的产出
- 上下文预算控制：防止下游阶段因接收太多信息导致 token 膨胀
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent.team.context")


# ── 角色上下文依赖图（最小上下文核心） ─────────────────

# 定义每个角色需要哪些上游阶段的产出
# key = 角色名, value = 需要的阶段 ID 列表（pipeline_builder 中的 stage 标识）
ROLE_CONTEXT_DEPS: dict[str, list[str]] = {
    "产品经理": [],                             # 产品经理是第一棒
    "软件架构师": ["requirements"],              # 架构师只需要需求
    "代码工程师": ["architecture"],             # 工程师只需要架构
    "测试工程师": ["requirements", "implementation"],  # 测试需要需求+代码
    "安全审查师": ["architecture", "implementation"],  # 安全需要架构+代码
    "DevOps工程师": ["implementation"],         # DevOps 需要代码（知道用什么技术栈）
    "文档专员": ["architecture", "implementation"],  # 文档需要架构+代码
}

# 角色上下文 Token 预算（防止单个角色上下文膨胀）
ROLE_TOKEN_BUDGET: dict[str, int] = {
    "产品经理": 4000,
    "软件架构师": 6000,
    "代码工程师": 8000,
    "测试工程师": 6000,
    "安全审查师": 4000,
    "DevOps工程师": 4000,
    "文档专员": 4000,
}

# 上下文注入限制（每个来源最多取 N 字符）
CONTEXT_SOURCE_CHARS_LIMIT: dict[str, int] = {
    "requirements": 3000,
    "architecture": 4000,
    "implementation": 6000,
    "testing": 2000,
    "security": 2000,
    "deployment": 2000,
    "documentation": 2000,
}


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
        logger.debug(f"团队黑板更新: {key} = {preview}")

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
        logger.debug(f"团队消息: {from_member} -> {to_member}: {content[:80]}")

    # ── v2.0: 最小上下文（根据角色加载相关上游产出） ──

    def get_context_for_member(self, member_name: str, stage: str = "") -> str:
        """获取成员所需的最小上下文

        只包含成员角色所需的上游阶段产出，避免不相关信息。

        Args:
            member_name: 成员角色名
            stage: 当前阶段标识（用于更精确的上下文选择）

        Returns:
            注入给该成员的 prompt 片段
        """
        parts = []
        budget = ROLE_TOKEN_BUDGET.get(member_name, 6000)
        current_chars = 0

        def _maybe_add(text: str) -> bool:
            """尝试添加文本，若超预算则截断"""
            nonlocal current_chars
            if current_chars >= budget:
                return False
            available = budget - current_chars
            if len(text) > available:
                text = text[:available] + "\n... [根据 Token 预算截断]"
            parts.append(text)
            current_chars += len(text)
            return True

        # 1. 黑板信息（所有角色都需要）
        blackboard_info = self._get_blackboard_compact()
        if blackboard_info:
            _maybe_add(blackboard_info)

        # 2. 原始任务（所有角色都需要）
        _maybe_add(f"## 团队任务\n{self.original_task}")

        # 3. 上游产出（按角色依赖图筛选）— v2.0 核心改进
        needed_stages = self._get_needed_stages(member_name, stage)
        if needed_stages:
            added_stages = []
            for stage_id in needed_stages:
                if stage_id in self.node_results:
                    result = self.node_results[stage_id]
                    if result:
                        limit = CONTEXT_SOURCE_CHARS_LIMIT.get(stage_id, 3000)
                        truncated = result[:limit].replace("\n", " ").strip()
                        if len(result) > limit:
                            truncated += "..."
                        added_stages.append(f"- **{stage_id}**: {truncated}")
                        # 也添加文件索引
                        output_key = f"{stage_id}_output"
                        if output_key in self.blackboard:
                            added_stages.append(f"  📄 完整产出: `{self.blackboard[output_key]}`")
            if added_stages:
                _maybe_add("## 上游阶段产出摘要\n" + "\n".join(added_stages))

        # 4. 产出文件索引
        artifact_index = self._get_artifact_index()
        if artifact_index:
            _maybe_add(artifact_index)

        # 5. 团队成员消息
        msgs = [m for m in self.messages if m.to_member == member_name or not m.to_member]
        if msgs:
            msg_text = "## 团队消息\n" + "\n".join(
                f"- 来自 {m.from_member}: {m.content[:500]}" for m in msgs[-5:]
            )
            _maybe_add(msg_text)

        # 6. Leader 反馈
        if self._leader_feedback:
            _maybe_add(f"## Leader 反馈\n{self._leader_feedback}")

        # 7. 反馈循环（仅测试和开发阶段需要）
        if self.feedback_loop.iteration > 0 and member_name in ("测试工程师", "代码工程师"):
            _maybe_add(self.feedback_loop.to_context_string())

        _maybe_add(f"\n当前执行轮次: 第 {self.iteration} 轮")
        return "\n\n".join(parts)

    def _get_needed_stages(self, member_name: str, stage: str = "") -> list[str]:
        """获取角色所需的上游阶段列表

        优先使用角色依赖图，如果有当前阶段信息则根据阶段自动推断。
        """
        # 精确匹配
        if member_name in ROLE_CONTEXT_DEPS:
            return ROLE_CONTEXT_DEPS[member_name]

        # 根据当前阶段推断需要哪些上游
        stage_to_deps = {
            "requirements": [],
            "architecture": ["requirements"],
            "implementation": ["architecture"],
            "testing": ["requirements", "implementation"],
            "security": ["architecture", "implementation"],
            "deployment": ["implementation"],
            "documentation": ["architecture", "implementation"],
        }
        if stage in stage_to_deps:
            return stage_to_deps[stage]

        # 回退：所有阶段
        return list(self.node_results.keys())

    # ── 黑板信息精简 ──────────────────────────────

    def _get_blackboard_compact(self) -> str:
        """精简版黑板：只输出索引信息，不输出大段内容"""
        if not self.blackboard:
            return ""
        skip_keys = {k for k in self.blackboard if k.endswith("_result") or k.endswith("_output")}
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

    # ── Leader 反馈 ───────────────────────────────

    def set_leader_feedback(self, feedback: str):
        self._leader_feedback = feedback

    def get_summary(self) -> str:
        """获取团队执行摘要（用于 Leader 审核）"""
        lines = [f"# 团队执行摘要 (第 {self.iteration} 轮)\n"]
        for node_id, result in self.node_results.items():
            lines.append(f"## {node_id}\n{result[:500] if result else '(空)'}\n")
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
