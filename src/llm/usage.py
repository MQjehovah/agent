import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("agent.usage")

# 模型定价（每百万 token，单位：元）—— 作为 settings.cost.pricing 未配置时的兜底。
# 真实价格请在 config.json 的 cost.pricing 中按模型覆写（如 {"glm-5": {"input": 0.5, "output": 1.5}}）。
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


def _resolve_pricing(model: str) -> dict:
    """优先 settings.cost.pricing；未配置或 settings 未初始化时回退 MODEL_PRICING。"""
    try:
        from settings import get_settings
        pricing_map = get_settings().get("cost.pricing")
        if isinstance(pricing_map, dict):
            p = pricing_map.get(model)
            if isinstance(p, dict) and "input" in p and "output" in p:
                return p
    except RuntimeError:
        pass
    except Exception:  # noqa: BLE001
        pass
    return MODEL_PRICING.get(model, {"input": 0, "output": 0})


def _resolve_attribution() -> tuple[str, str, str]:
    """从当前 run 上下文解析归因 (user_id, session_id, agent_id)。

    延迟导入 agent.current_run 避免循环（agent 不导入 usage/llm）。
    asyncio Task 自动复制 contextvar，故后台任务（如反思）继承父级归因。
    run 之外（如 autonomous 系统调用）user_id 为空 → 归因 'system'。
    """
    try:
        from agent.core import current_run
        rc = current_run()
        user_id = rc.user_id or "system"
        session_id = rc.session.session_id if rc.session else ""
        agent_id = rc.agent_id or ""
        return user_id, session_id, agent_id
    except Exception:  # noqa: BLE001
        return "system", "", ""


@dataclass
class UsageRecord:
    timestamp: datetime
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    duration_ms: float = 0.0
    is_stream: bool = False
    user_id: str = "system"
    session_id: str = ""
    agent_id: str = ""


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

        pricing = _resolve_pricing(model)
        cost = (prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]) / 1_000_000

        duration_ms = 0.0
        if self._call_start > 0:
            duration_ms = (time.monotonic() - self._call_start) * 1000
            self._call_start = 0.0

        user_id, session_id, agent_id = _resolve_attribution()
        record = UsageRecord(
            timestamp=datetime.now(),
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            duration_ms=duration_ms,
            is_stream=is_stream,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
        )
        self.records.append(record)
        logger.debug(
            f"[用量] {model}: {prompt_tokens}+{completion_tokens} tokens, "
            f"¥{cost:.4f}, {duration_ms:.0f}ms [user={user_id}]"
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

    def get_summary_by_user(self) -> dict[str, dict]:
        return self._aggregate(lambda r: r.user_id)

    def get_summary_by_session(self) -> dict[str, dict]:
        return self._aggregate(lambda r: r.session_id)

    def get_summary_by_agent(self) -> dict[str, dict]:
        return self._aggregate(lambda r: r.agent_id)

    def _aggregate(self, key_fn) -> dict[str, dict]:
        buckets = defaultdict(lambda: {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0,
        })
        for r in self.records:
            b = buckets[key_fn(r)]
            b["calls"] += 1
            b["prompt_tokens"] += r.prompt_tokens
            b["completion_tokens"] += r.completion_tokens
            b["cost"] += r.cost
        return dict(buckets)

    def flush(self) -> int:
        """将内存记录批量持久化到 storage；返回写入条数（storage 不可用时返回 0，记录留在内存）。"""
        if not self.records:
            return 0
        try:
            from storage.storage import get_storage
            storage = get_storage()
            if not storage or not hasattr(storage, "save_usage_batch"):
                return 0
        except Exception:  # noqa: BLE001
            return 0

        batch = [{
            "user_id": r.user_id, "session_id": r.session_id, "agent_id": r.agent_id,
            "model": r.model, "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens, "cost": r.cost,
            "is_stream": r.is_stream, "ts": r.timestamp.isoformat(),
        } for r in self.records]
        try:
            written = storage.save_usage_batch(batch)
            if written > 0:
                del self.records[:written]
            return written
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[用量] flush 失败: {e}")
            return 0

    def reset(self):
        self.records.clear()
