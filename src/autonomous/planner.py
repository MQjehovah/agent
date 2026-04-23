import json
import logging
import re

from autonomous.goal import Goal, Plan, PlanStep

logger = logging.getLogger("agent.autonomous.planner")

PLAN_PROMPT = """\
你是一个任务规划专家。请将以下目标分解为可执行的步骤。

目标：{title}
描述：{description}
{context_section}\
{tools_section}
\
请返回JSON格式的步骤列表：
{{"steps": [{{"task": "步骤描述", "requires_confirmation": false}}, ...]}}

要求：
- 简单目标（如查询、搜索、问答）只需1个步骤，不要拆分
- 中等复杂度目标拆成2-3步
- 复杂目标（涉及多个系统、多人协作）拆成3-5步，最多不超过5步
- 每个步骤必须包含足够的上下文信息，确保独立执行时能理解完整任务
- 仅在涉及删除数据、发送外部消息等高风险操作时设置 requires_confirmation 为 true
- 只返回JSON，不要其他内容
"""

REPLAN_PROMPT = """\
你是一个任务规划专家。之前的执行计划遇到了问题，请根据反馈重新规划。

目标：{title}
描述：{description}

已完成的步骤：
{completed_section}

失败的步骤：
{failed_section}

反馈：{feedback}
{tools_section}

请返回JSON格式的步骤列表：
{{"steps": [{{"task": "步骤描述", "requires_confirmation": false}}, ...]}}

要求：
- 根据反馈调整计划，避免重复已成功完成的步骤（除非必要）
- 重点解决失败的问题
- 只返回JSON，不要其他内容
"""


class Planner:
    def __init__(self, client, tool_summary: str = "", subagent_summary: str = ""):
        self.client = client
        self.tool_summary = tool_summary
        self.subagent_summary = subagent_summary

    async def plan(self, goal: Goal, context: str = "") -> Plan:
        context_section = f"上下文：{context}\n" if context else ""
        tools_section = self._build_tools_section()

        prompt = PLAN_PROMPT.format(
            title=goal.title,
            description=goal.description,
            context_section=context_section,
            tools_section=tools_section,
        )
        steps = await self._call_llm(prompt)
        return Plan(goal_id=goal.id, steps=steps)

    async def replan(
        self,
        goal: Goal,
        feedback: str,
        completed_steps: list,
        failed_steps: list,
    ) -> Plan:
        completed_section = self._format_steps(completed_steps) if completed_steps else "无"
        failed_section = self._format_steps(failed_steps) if failed_steps else "无"
        tools_section = self._build_tools_section()

        prompt = REPLAN_PROMPT.format(
            title=goal.title,
            description=goal.description,
            completed_section=completed_section,
            failed_section=failed_section,
            feedback=feedback,
            tools_section=tools_section,
        )
        steps = await self._call_llm(prompt)
        return Plan(goal_id=goal.id, steps=steps)

    async def _call_llm(self, prompt: str) -> list[PlanStep]:
        messages = [{"role": "user", "content": prompt}]
        response = await self.client.chat(messages, tools=None, stream=False)
        content = response.choices[0].message.content
        return self._parse_steps(content)

    def _parse_steps(self, content: str) -> list[PlanStep]:
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = self._extract_json(content)

        if data is None:
            logger.warning("无法解析LLM返回的步骤列表: %s", content[:200])
            return []

        steps_data = data.get("steps", [])
        steps = []
        for i, s in enumerate(steps_data):
            step = PlanStep(
                plan_id="",
                task_description=s.get("task", str(s)),
                order=i,
                requires_confirmation=s.get("requires_confirmation", False),
            )
            steps.append(step)
        return steps

    def _extract_json(self, content: str) -> dict | None:
        match = re.search(r"\{[\s\S]*\}", content)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return None

    def _format_steps(self, steps: list) -> str:
        lines = []
        for s in steps:
            desc = s.task_description if hasattr(s, "task_description") else str(s)
            status = s.status if hasattr(s, "status") else "unknown"
            result = s.result if hasattr(s, "result") and s.result else ""
            line = f"- [{status}] {desc}"
            if result:
                line += f" (结果: {result})"
            lines.append(line)
        return "\n".join(lines)

    def _build_tools_section(self) -> str:
        parts = []
        if self.tool_summary:
            parts.append(f"可用工具：{self.tool_summary}")
        if self.subagent_summary:
            parts.append(f"可用子代理：{self.subagent_summary}")
        return "\n".join(parts) + "\n" if parts else ""
