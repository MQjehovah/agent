import json
import logging
import os
import re
import uuid
from typing import Any

from team.context import TeamContext
from team.dag import ExecutionDAG

logger = logging.getLogger("agent.team.orchestrator")


def _extract_json_from_llm(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
    return content.strip()


# 固定阶段流水线顺序
PIPELINE = [
    {"stage": "requirements", "role": "产品经理", "output": "requirements.md"},
    {"stage": "architecture", "role": "软件架构师", "output": "architecture.md"},
    {"stage": "implementation", "role": "代码工程师", "output": None},
    {"stage": "testing", "role": "测试工程师", "output": "test_report.md"},
    {"stage": "security", "role": "安全审查师", "output": "security_report.md"},
    {"stage": "deployment", "role": "DevOps工程师", "output": None},
    {"stage": "documentation", "role": "文档专员", "output": None},
]


class TeamOrchestrator:
    def __init__(
        self,
        team_name: str,
        team_config: dict[str, Any],
        members: dict[str, dict[str, Any]],
        subagent_manager,
        llm_client,
        memory_manager=None,
    ):
        self.team_name = team_name
        self.config = team_config
        self.members = members
        self.leader = team_config.get("leader", "")
        self.subagent_manager = subagent_manager
        self.llm = llm_client
        self.memory = memory_manager
        self.context: TeamContext | None = None
        self.dag: ExecutionDAG | None = None
        self.project_dir: str = ""
        self.artifacts: dict[str, str] = {}  # stage → file_path

    async def run(self, task: str) -> str:
        self.context = TeamContext(self.team_name, task)
        self._setup_project_dir(task)
        self.context.set_blackboard("项目路径", self.project_dir)
        self.context.set_blackboard("团队名称", self.team_name)

        # 阶段化流水线执行
        stage_order = [s for s in PIPELINE if s["role"] in self.members]
        for stage in stage_order:
            role = stage["role"]
            stage_name = stage["stage"]
            output_file = stage["output"]

            logger.info(f"团队 [{self.team_name}] 阶段 [{stage_name}] -> {role}")

            result = await self._run_stage(role, stage_name, output_file)
            if result is None:
                continue

            if result.startswith("ERROR:"):
                self.context.set_blackboard(f"{stage_name}_error", result)
                logger.warning(f"阶段 [{stage_name}] 失败: {result}")

            # 自动传递产出物：将文件路径写入 blackboard 供下游使用
            if output_file:
                artifact_path = os.path.join(self.project_dir, output_file)
                self.artifacts[stage_name] = artifact_path
                self.context.set_blackboard(f"{stage_name}_output", artifact_path)
                self.context.set_blackboard(
                    f"{stage_name}_result",
                    self._read_file_head(artifact_path, 3000),
                )

            # 关键节点 Leader 审核
            if stage_name in ("requirements", "implementation", "security"):
                confirmed, feedback = await self._leader_review()
                if not confirmed:
                    logger.info(f"Leader 在 [{stage_name}] 阶段要求修改: {feedback[:100]}")
                    self.context.set_leader_feedback(feedback)
                    # 重试当前阶段
                    retry_result = await self._run_stage(role, f"{stage_name}_retry", output_file)
                    if retry_result and retry_result.startswith("ERROR:"):
                        self.context.set_blackboard(f"{stage_name}_retry_error", retry_result)

        return self._build_report()

    def _setup_project_dir(self, task: str):
        path = self._extract_project_path(task)
        if path:
            self.project_dir = path
        else:
            workspace = self.config.get("workspace", "")
            projects_dir = os.path.join(workspace, "projects")
            os.makedirs(projects_dir, exist_ok=True)
            self.project_dir = os.path.join(projects_dir, uuid.uuid4().hex[:8])
        os.makedirs(self.project_dir, exist_ok=True)
        logger.info(f"项目路径: {self.project_dir}")

    def _extract_project_path(self, task: str) -> str:
        patterns = [
            r'在\s*[`"]?([^`"\s]+)[`"]?\s*项目中',
            r'项目[路径目录][:：]\s*[`"]?([^`"\s]+)[`"]?',
        ]
        for p in patterns:
            m = re.search(p, task)
            if m:
                return m.group(1) if m.lastindex else m.group(0)
        return ""

    async def _run_stage(self, role: str, stage: str, output_file: str | None) -> str | None:
        """运行单个阶段，返回结果或 None"""
        stage_context = self.context.get_context_for_member(role)

        # 自动注入上游产出物
        upstream = self._get_upstream_artifacts(role)
        full_task = stage_context
        if upstream:
            full_task += "\n\n## 上游产出物（请先阅读）\n" + upstream

        full_task += f"\n\n## 你的任务\n根据你的角色职责完成「{stage}」阶段的工作。"
        if output_file:
            full_task += f"\n将输出写入 `{output_file}`。"
        full_task += f"\n工作目录: {self.project_dir}"

        try:
            result = await self.subagent_manager.run_team_agent(
                team_name=self.team_name,
                member_name=role,
                task=full_task,
            )
            self.context.add_node_result(stage, role, result)
            self._save_to_memory(stage, role, result)
            return result
        except Exception as e:
            logger.error(f"阶段 [{stage}] 异常: {e}")
            return f"ERROR: {e}"

    def _get_upstream_artifacts(self, role: str) -> str:
        """获取当前阶段的上游产出物内容"""
        parts = []
        for s in PIPELINE:
            if s["role"] == role:
                break
            if s["stage"] in self.artifacts:
                path = self.artifacts[s["stage"]]
                if os.path.exists(path):
                    content = self._read_file_head(path, 5000)
                    if content:
                        parts.append(f"### {s['stage']} 产出 ({s['output']})\n{content}")
        return "\n\n".join(parts) if parts else ""

    @staticmethod
    def _read_file_head(path: str, max_chars: int) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars)
            if len(content) == max_chars:
                content += "\n... [截断]"
            return content
        except Exception:
            return ""

    async def _leader_review(self) -> tuple[bool, str]:
        if not self.leader or self.leader not in self.members:
            return True, ""

        summary = self.context.get_summary()
        leader_prompt = self.config.get("leader_prompt", "")
        leader_context = ""
        if leader_prompt:
            leader_context = f"\n## Leader 角色说明\n{leader_prompt}\n"

        prompt = f"""你是团队 "{self.team_name}" 的 Leader ({self.leader})。
{leader_context}
请对当前阶段产出进行多维度审核。

## 当前产出
{summary}

## 原始任务
{self.context.original_task}

请返回多维度评分 JSON:
{{"confirmed": true/false,
  "scores": {{"功能": 0-10, "代码质量": 0-10, "测试": 0-10, "安全": 0-10, "性能": 0-10, "文档": 0-10}},
  "feedback": "如果不通过，说明问题和由谁修改"}}

只有核心功能完整、无明显安全漏洞时 confirmed 才为 true。
只返回 JSON。"""

        try:
            resp = await self.llm.chat([{"role": "user", "content": prompt}])
            content = resp.choices[0].message.content if hasattr(resp, "choices") else str(resp)
            result = json.loads(_extract_json_from_llm(content))
            scores = result.get("scores", {})
            logger.info(f"Leader 审核: confirmed={result.get('confirmed')}, scores={scores}")
            return result.get("confirmed", True), result.get("feedback", "")
        except Exception as e:
            logger.warning(f"Leader 审核解析失败: {e}")
            return True, ""

    def _save_to_memory(self, stage: str, role: str, result: str):
        if not self.memory:
            return
        try:
            self.memory.share_knowledge(
                from_agent=f"{self.team_name}/{role}",
                knowledge=f"[{stage}] {result[:500]}",
            )
        except Exception as e:
            logger.warning(f"保存记忆失败: {e}")

    def _build_report(self) -> str:
        parts = [
            f"# 团队执行报告: {self.team_name}",
            f"## 原始任务\n{self.context.original_task}",
            f"## 项目目录\n{self.project_dir}",
            "## 流水线阶段",
        ]
        for stage in PIPELINE:
            if stage["role"] not in self.members:
                continue
            result_key = f"{stage['stage']}_result"
            output = self.context.blackboard.get(result_key, "")
            status = "✓" if output and not output.startswith("ERROR:") else "✗"
            parts.append(f"- {status} {stage['stage']} ({stage['role']})")
            if output and len(output) > 200:
                output = output[:200] + "..."
            if output:
                parts.append(f"  {output}")

        parts.append(f"\n## 产出物\n{self.project_dir}/")
        for f in os.listdir(self.project_dir) if os.path.exists(self.project_dir) else []:
            parts.append(f"  - {f}")

        return "\n\n".join(parts)
