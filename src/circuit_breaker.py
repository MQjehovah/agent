"""
渐进式熔断器 — 防止 Agent 陷入无限重试循环

设计思路（参考 Hystrix / grok-build anti-deadlock）：
- 3 种状态：closed（正常）→ half-open（尝试恢复）→ open（熔断）
- 分级降级：连续失败次数越多，降级策略越保守
- 冷却时间：熔断后等待一段时间再尝试恢复
- 成功复位：一次成功后计数器归零

用法:
    cb = CircuitBreaker(name="tool_execute", threshold=5, cooldown=30)

    async def execute_with_protection():
        if not cb.allow_request():
            return fallback_result()
        try:
            result = await do_something()
            cb.on_success()
            return result
        except Exception as e:
            cb.on_failure()
            return cb.get_fallback(e)
"""
import logging
import time
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("agent.circuit_breaker")


class CircuitState(Enum):
    CLOSED = "closed"        # 正常状态，请求正常通过
    HALF_OPEN = "half-open"   # 半开状态，允许试探性请求
    OPEN = "open"            # 熔断状态，请求被快速拒绝


# 全局熔断器注册表（用于统一查看和管理）
_registry: dict[str, "CircuitBreaker"] = {}


def get_registry() -> dict[str, "CircuitBreaker"]:
    return dict(_registry)


class CircuitBreaker:
    """渐进式熔断器"""

    def __init__(
        self,
        name: str = "default",
        threshold: int = 5,           # 连续失败 N 次后熔断
        half_open_threshold: int = 2,  # 半开状态允许的试探请求数
        cooldown: float = 30.0,        # 熔断冷却时间（秒）
        half_open_cooldown: float = 5.0,  # 半开状态下的重试间隔
        recovery_successes: int = 2,   # 半开后连续成功 N 次才算恢复
    ):
        self.name = name
        self.threshold = threshold
        self.half_open_threshold = half_open_threshold
        self.cooldown = cooldown
        self.half_open_cooldown = half_open_cooldown
        self.recovery_successes = recovery_successes

        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self.total_failures = 0
        self.total_successes = 0
        self.last_failure_time = 0.0
        self.last_success_time = 0.0
        self.state_changed_at = time.time()
        self._half_open_attempts = 0
        self._last_exception: Optional[Exception] = None

        # 注册到全局
        _registry[name] = self

    # ── 核心接口 ───────────────────────────────────────

    def allow_request(self) -> bool:
        """判断当前请求是否允许通过

        Returns:
            True=允许执行, False=应走降级路径
        """
        now = time.time()

        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if now - self.state_changed_at >= self.cooldown:
                logger.info(f"[熔断器:{self.name}] 冷却结束，进入 half-open")
                self.state = CircuitState.HALF_OPEN
                self._half_open_attempts = 0
                self.state_changed_at = now
                return True
            return False

        # HALF_OPEN
        if self._half_open_attempts >= self.half_open_threshold:
            if now - self.state_changed_at < self.half_open_cooldown:
                return False
            self._half_open_attempts = 0
        if now - self.last_failure_time < self.half_open_cooldown:
            return False
        self._half_open_attempts += 1
        return True

    def on_success(self):
        """记录一次成功"""
        self.consecutive_failures = 0
        self.consecutive_successes += 1
        self.total_successes += 1
        self.last_success_time = time.time()
        self._last_exception = None

        if self.state == CircuitState.HALF_OPEN:
            if self.consecutive_successes >= self.recovery_successes:
                logger.info(f"[熔断器:{self.name}] 恢复成功 {self.consecutive_successes} 次，回到 closed")
                self.state = CircuitState.CLOSED
                self.consecutive_successes = 0
                self.state_changed_at = time.time()

    def on_failure(self, exception: Optional[Exception] = None):
        """记录一次失败"""
        self.consecutive_failures += 1
        self.consecutive_successes = 0
        self.total_failures += 1
        self.last_failure_time = time.time()
        self._last_exception = exception

        if self.state == CircuitState.HALF_OPEN:
            # 半开状态下失败 → 立即回到熔断
            logger.warning(f"[熔断器:{self.name}] half-open 下失败，回到 open")
            self.state = CircuitState.OPEN
            self.state_changed_at = time.time()
            return

        if self.consecutive_failures >= self.threshold and self.state == CircuitState.CLOSED:
            logger.warning(
                f"[熔断器:{self.name}] 连续 {self.consecutive_failures} 次失败，熔断开启 "
                f"(冷却 {self.cooldown}s)"
            )
            self.state = CircuitState.OPEN
            self.state_changed_at = time.time()

    # ── 降级策略 ───────────────────────────────────────

    def get_fallback(self, exception: Optional[Exception] = None) -> str:
        """根据失败次数返回不同的降级响应

        分级降级策略:
        - 1-2 次失败: 重试建议
        - 3-4 次失败: 简化工具集
        - 5+ 次失败: 完全降级
        """
        if exception:
            self._last_exception = exception

        fails = self.consecutive_failures

        if fails >= 5:
            return json_fallback("error",
                "工具多次失败，已完全降级。建议改用其他方式完成当前操作。",
                level="circuit_open")
        elif fails >= 3:
            return json_fallback("warning",
                f"工具连续 {fails} 次失败，建议尝试不同的方法或使用更简单的操作。",
                level="degraded")
        else:
            return json_fallback("retry",
                f"工具执行出错: {self._last_exception}，请检查参数后重试。",
                level="normal")

    # ── 查询接口 ───────────────────────────────────────

    def reset(self):
        """手动重置熔断器"""
        self.state = CircuitState.CLOSED
        self.consecutive_failures = 0
        self.consecutive_successes = 0
        self._half_open_attempts = 0
        self.state_changed_at = time.time()
        logger.info(f"[熔断器:{self.name}] 手动重置")

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    @property
    def is_closed(self) -> bool:
        return self.state == CircuitState.CLOSED

    @property
    def failure_rate(self) -> float:
        total = self.total_failures + self.total_successes
        if total == 0:
            return 0.0
        return self.total_failures / total

    def get_stats(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "failure_rate": round(self.failure_rate, 3),
            "threshold": self.threshold,
            "cooldown": self.cooldown,
            "last_failure_ago_s": round(time.time() - self.last_failure_time, 1) if self.last_failure_time else None,
            "last_success_ago_s": round(time.time() - self.last_success_time, 1) if self.last_success_time else None,
        }


def json_fallback(status: str, message: str, level: str = "normal") -> str:
    """生成 JSON 降级响应"""
    import json
    return json.dumps({
        "success": False,
        "status": status,
        "error": message,
        "circuit_breaker": level,
    }, ensure_ascii=False)
