"""失败分类与回退策略"""
from dataclasses import dataclass


@dataclass
class FallbackAction:
    stage: str = "unknown"
    message: str = ""
    suggestion: str = ""


class CompileError(FallbackAction):
    stage: str = "code"

    def __init__(self, message: str = ""):
        self.message = message
        self.suggestion = self._extract_suggestion()

    def _extract_suggestion(self) -> str:
        if "undefined reference" in self.message:
            return f"Missing link: {self.message}"
        if "syntax error" in self.message:
            return "Syntax error in code"
        return "Compile failure, review code and fix errors"


class TestError(FallbackAction):
    stage: str = "test"

    def __init__(self, message: str = ""):
        self.message = message
        self.suggestion = self._extract_suggestion()

    def _extract_suggestion(self) -> str:
        return f"Test did not pass: {self.message}"


def classify_failure(result: str) -> FallbackAction:
    """Classify a failure result and determine the appropriate fallback action."""
    result_lower = result.lower()
    if any(kw in result_lower for kw in ["编译错误", "compile error", "undefined reference", "syntax error"]):
        return CompileError(message=result)
    if any(kw in result_lower for kw in ["测试失败", "test fail", "test error", "assert"]):
        return TestError(message=result)
    return FallbackAction(stage="unknown", message=result)
