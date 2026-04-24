"""Pipeline DAG 定义"""
from dataclasses import dataclass, field


@dataclass
class RetryPolicy:
    """重试策略"""
    max_retries: int = 3
    fallback_to_user: bool = True


@dataclass
class PipelineStage:
    """流水线阶段"""
    name: str
    agent: str
    description: str = ""
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    attempt_count: int = 0
    result: str | None = None
    success: bool = False

    def should_retry(self) -> bool:
        """检查是否应该重试"""
        return self.attempt_count < self.retry_policy.max_retries

    def record_attempt(self, success: bool, result: str = ""):
        """记录一次执行尝试"""
        self.attempt_count += 1
        self.success = success
        if success:
            self.result = result


@dataclass
class Pipeline:
    """流水线 DAG"""
    stages: list[PipelineStage]
    _current_index: int = 0

    @property
    def current_stage(self) -> PipelineStage | None:
        """当前阶段"""
        if self._current_index >= len(self.stages):
            return None
        return self.stages[self._current_index]

    def advance(self):
        """前进到下一阶段"""
        self._current_index += 1

    def is_complete(self) -> bool:
        """流水线是否已完成"""
        return self._current_index >= len(self.stages)

    def reset_to(self, stage_name: str):
        """重置到指定阶段"""
        for i, stage in enumerate(self.stages):
            if stage.name == stage_name:
                self._current_index = i
                stage.attempt_count = 0
                stage.success = False
                stage.result = None
                return
