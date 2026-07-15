"""评估回归框架：golden cases + LLM-as-judge（P4a）。

设计要点：
- 复用 Agent.run() 驱动被测 Agent，LLMClient.chat() 做评审，parse_llm_json 解析分。
- 评审 prompt 要求 JSON {score, reason}，与反思/curator 的结构化范式一致。
- 真实评估（连真实 LLM）的用例应标记 @pytest.mark.eval 并默认跳过，
  避免成本/非确定性进入常规 CI；本文件本身只提供框架，不绑定真实端点。
"""
from collections.abc import Callable
from dataclasses import dataclass, field

JUDGE_SYSTEM = "你是 Agent 回答质量评估助手。只输出JSON，不要输出其他内容。"
JUDGE_PROMPT = (
    "根据以下标准评估 Agent 的回答质量。\n\n"
    "任务: {task}\n"
    "评估标准:\n{criteria}\n"
    "Agent 回答:\n{output}\n\n"
    "严格按以下JSON格式输出：\n"
    '{{"score": 1-5, "reason": "<评分理由>"}}'
)


@dataclass
class GoldenCase:
    task: str
    criteria: list[str]
    must_contain: list[str] = field(default_factory=list)
    id: str = ""


@dataclass
class EvalResult:
    case_id: str
    output: str
    hard_pass: bool
    judge: dict | None = None

    @property
    def passed(self) -> bool:
        if not self.hard_pass:
            return False
        if self.judge is None:
            return True
        try:
            return int(self.judge.get("score", 0)) >= 4
        except (TypeError, ValueError):
            return False


class EvalRunner:
    """驱动 Agent 跑 golden case，并用 LLM-as-judge 打分。"""

    def __init__(self, agent_factory: Callable, judge_client=None):
        self.agent_factory = agent_factory
        self.judge_client = judge_client

    async def run_case(self, case: GoldenCase) -> EvalResult:
        agent = self.agent_factory()
        result = await agent.run(case.task)
        output = getattr(result, "result", None) or str(result)
        hard_pass = all(s in output for s in case.must_contain)
        judge = await self._judge(case, output) if self.judge_client else None
        return EvalResult(
            case_id=case.id or case.task[:30],
            output=output, hard_pass=hard_pass, judge=judge,
        )

    async def run_cases(self, cases: list[GoldenCase]) -> list[EvalResult]:
        return [await self.run_case(c) for c in cases]

    async def _judge(self, case: GoldenCase, output: str) -> dict | None:
        from autonomous import parse_llm_json
        prompt = JUDGE_PROMPT.format(
            task=case.task[:300],
            criteria="\n".join(f"- {c}" for c in case.criteria),
            output=output[:2000],
        )
        try:
            resp = await self.judge_client.chat(
                messages=[{"role": "system", "content": JUDGE_SYSTEM},
                          {"role": "user", "content": prompt}],
                tools=None, stream=False, use_cache=False,
            )
            text = resp.choices[0].message.content or ""
            data = parse_llm_json(text)
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            return None
