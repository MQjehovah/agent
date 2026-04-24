"""失败分类与回退策略 单元测试"""
import sys

sys.path.insert(0, "src")

from src.team.errors import CompileError, FallbackAction, TestError, classify_failure


def test_classify_compile_error():
    result = classify_failure("编译错误: undefined reference to 'ORBextractor'")
    assert isinstance(result, CompileError)
    assert result.stage == "code"


def test_classify_test_error():
    result = classify_failure("测试失败: 5/10 passed, expected 10/10")
    assert isinstance(result, TestError)
    assert result.stage == "test"


def test_classify_unknown():
    result = classify_failure("未知错误")
    assert isinstance(result, FallbackAction)
    assert result.stage == "unknown"


def test_compile_error_suggests_fix():
    err = CompileError("undefined reference to 'ORBextractor'")
    assert "ORBextractor" in err.suggestion


def test_test_error_suggestion():
    err = TestError("测试未通过: 期望 0.5, 实际 0.3")
    assert "测试未通过" in err.suggestion


def test_classify_multiple_compile_patterns():
    """Verify multiple patterns all classify as compile errors"""
    patterns = [
        "Compile error: syntax error at line 42",
        "compile error: missing semicolon",
        "undefined reference to 'foo'",
        "syntax error: unexpected token",
    ]
    for p in patterns:
        result = classify_failure(p)
        assert isinstance(result, CompileError), f"Failed for: {p}"


def test_classify_multiple_test_patterns():
    patterns = [
        "Test failure: expected 5, got 3",
        "test error: timeout after 30s",
        "AssertionError: values not equal",
    ]
    for p in patterns:
        result = classify_failure(p)
        assert isinstance(result, TestError), f"Failed for: {p}"
