import logging
import time
from collections import defaultdict
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
    duration_ms: float = 0.0
    is_stream: bool = False


@dataclass
class UsageTracker:
    records: list = field(default_factory=list)
    _call_start: float = 0.0

    def start_timer(self):
        self._call_start = time.monotonic()

    def track(self, model: str, usage: dict, is_stream: bool = False):
        if not usage:
            return

        prompt_tokens = usage.get("prompt_tokens", 0) or 0
        completion_tokens = usage.get("completion_tokens", 0) or 0

        pricing = MODEL_PRICING.get(model, {"input": 0, "output": 0})
        cost = (prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]) / 1_000_000

        duration_ms = 0.0
        if self._call_start > 0:
            duration_ms = (time.monotonic() - self._call_start) * 1000
            self._call_start = 0.0

        record = UsageRecord(
            timestamp=datetime.now(),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            duration_ms=duration_ms,
            is_stream=is_stream,
        )
        self.records.append(record)
        logger.debug(
            f"[用量] {model}: {prompt_tokens}+{completion_tokens} tokens, "
            f"¥{cost:.4f}, {duration_ms:.0f}ms"
        )

    def get_summary(self) -> dict:
        total_prompt = sum(r.prompt_tokens for r in self.records)
        total_completion = sum(r.completion_tokens for r in self.records)
        total_cost = sum(r.cost for r in self.records)
        total_duration = sum(r.duration_ms for r in self.records)
        durations = [r.duration_ms for r in self.records if r.duration_ms > 0]

        return {
            "total_calls": len(self.records),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "total_cost_cny": round(total_cost, 4),
            "total_duration_ms": total_duration,
            "avg_duration_ms": sum(durations) / len(durations) if durations else 0,
            "max_duration_ms": max(durations) if durations else 0,
            "min_duration_ms": min(durations) if durations else 0,
        }

    def get_per_model_summary(self) -> dict[str, dict]:
        per_model = defaultdict(lambda: {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "cost": 0.0, "duration_ms": 0.0, "stream_calls": 0,
        })
        for r in self.records:
            m = per_model[r.model]
            m["calls"] += 1
            m["prompt_tokens"] += r.prompt_tokens
            m["completion_tokens"] += r.completion_tokens
            m["cost"] += r.cost
            m["duration_ms"] += r.duration_ms
            if r.is_stream:
                m["stream_calls"] += 1
        return dict(per_model)

    def reset(self):
        self.records.clear()
