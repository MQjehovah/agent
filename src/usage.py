import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("agent.usage")

# 模型定价（每百万 token，单位：元）
MODEL_PRICING = {
    "gpt-4o": {"input": 17.5, "output": 70.0},
    "gpt-4o-mini": {"input": 1.05, "output": 4.2},
    "gpt-4-turbo": {"input": 70.0, "output": 210.0},
    "deepseek-chat": {"input": 1.0, "output": 2.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    "qwen-max": {"input": 20.0, "output": 60.0},
    "qwen-plus": {"input": 4.0, "output": 12.0},
    "qwen-turbo": {"input": 0.3, "output": 0.6},
    "glm-4": {"input": 14.0, "output": 14.0},
    "glm-4-flash": {"input": 0.1, "output": 0.1},
    "glm-5": {"input": 0.1, "output": 0.1},
}


@dataclass
class UsageRecord:
    timestamp: datetime
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: float


@dataclass
class UsageTracker:
    records: list = field(default_factory=list)

    def track(self, model: str, usage: dict):
        if not usage:
            return

        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0

        pricing = MODEL_PRICING.get(model, {"input": 0, "output": 0})
        cost = (prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]) / 1_000_000

        record = UsageRecord(
            timestamp=datetime.now(),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
        )
        self.records.append(record)
        logger.debug(
            f"[用量] {model}: {prompt_tokens}+{completion_tokens} tokens, ¥{cost:.4f}"
        )

    def get_summary(self) -> dict:
        total_prompt = sum(r.prompt_tokens for r in self.records)
        total_completion = sum(r.completion_tokens for r in self.records)
        total_cost = sum(r.cost for r in self.records)
        return {
            "total_calls": len(self.records),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost_cny": round(total_cost, 4),
        }

    def reset(self):
        self.records.clear()
