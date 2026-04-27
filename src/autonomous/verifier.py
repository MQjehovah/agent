import logging
from dataclasses import dataclass

from autonomous.goal import Goal, PlanStep
from autonomous import parse_llm_json

logger = logging.getLogger("agent.autonomous.verifier")

VERIFY_PROMPT = """\
你是一个执行结果验证专家。请判断以下步骤的执行结果是否达成了目标。

目标：{title}
描述：{description}

执行步骤及结果：
{steps_section}

请返回JSON格式的验证结果：
{{"passed": true/false, "confidence": 0.0-1.0, "summary": "结果摘要", "feedback": "改进建议（未通过时）"}}

要求：
- passed 表示是否达成目标
- confidence 为置信度（0.0到1.0）
- summary 简要概括执行结果
- feedback 在未通过时给出改进建议
- 只返回JSON，不要其他内容
"""


@dataclass
class VerificationResult:
    passed: bool
    confidence: float
    summary: str
    feedback: str = ""


class Verifier:
    def __init__(self, client):
        self.client = client

    async def verify(self, goal: Goal, steps: list[PlanStep]) -> VerificationResult:
        steps_section = self._format_steps(steps)
        prompt = VERIFY_PROMPT.format(
            title=goal.title,
            description=goal.description,
            steps_section=steps_section,
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            response = await self.client.chat(messages, tools=None, stream=False)
            content = response.choices[0].message.content
            return self._parse_result(content)
        except Exception as e:
            logger.warning("验证器LLM调用失败，使用降级判断: %s", e)
            return self._fallback_verify(steps)

    def _parse_result(self, content: str) -> VerificationResult:
        data = parse_llm_json(content)
        if data is None:
            logger.warning("无法解析验证结果: %s", content[:200])
            return self._fallback_verify([])

        return VerificationResult(
            passed=bool(data.get("passed", False)),
            confidence=float(data.get("confidence", 0.5)),
            summary=str(data.get("summary", "")),
            feedback=str(data.get("feedback", "")),
        )

    def _format_steps(self, steps: list[PlanStep]) -> str:
        lines = []
        for s in steps:
            line = f"- [{s.status}] {s.task_description}"
            if s.result:
                line += f" (结果: {s.result})"
            lines.append(line)
        return "\n".join(lines) if lines else "无"

    def _fallback_verify(self, steps: list[PlanStep]) -> VerificationResult:
        all_completed = all(s.status == "completed" for s in steps) if steps else False
        return VerificationResult(
            passed=all_completed,
            confidence=0.5,
            summary="降级验证：基于步骤完成状态判断",
            feedback="" if all_completed else "部分步骤未完成",
        )
