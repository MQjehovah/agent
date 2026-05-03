import logging
import re

logger = logging.getLogger("agent.team.feedback")


def parse_test_output(raw_output: str) -> list[dict]:
    """从测试输出中解析测试结果

    支持 pytest、unittest、jest、go test 格式
    """
    results = []

    # pytest 格式: FAILED test_foo.py::test_bar - Error
    # PASSED test_foo.py::test_bar
    pytest_pattern = re.compile(
        r"(PASSED|FAILED|ERROR)\s+(\S+)(?:\s*-\s*(.+))?",
        re.IGNORECASE,
    )
    for m in pytest_pattern.finditer(raw_output):
        results.append({
            "name": m.group(2),
            "passed": m.group(1).upper() in ("PASSED",),
            "details": m.group(3) or "",
        })

    if results:
        return results

    # pytest summary: X failed, Y passed, Z skipped
    summary_pattern = re.compile(
        r"(\d+)\s+failed.*?(\d+)\s+passed",
        re.IGNORECASE | re.DOTALL,
    )
    m = summary_pattern.search(raw_output)
    if m:
        failed = int(m.group(1))
        passed = int(m.group(2))
        results.append({
            "name": "pytest summary",
            "passed": failed == 0 and passed > 0,
            "details": f"{failed} failed, {passed} passed",
        })
        return results

    # go test: PASS / FAIL
    go_pattern = re.compile(r"^(PASS|FAIL)\s*$", re.MULTILINE)
    for m in go_pattern.finditer(raw_output):
        results.append({
            "name": "go test",
            "passed": m.group(1) == "PASS",
            "details": "",
        })
    if results:
        return results

    # jest: Tests: X failed, Y passed, Z total
    jest_pattern = re.compile(
        r"Tests:\s*(\d+)\s+failed.*?(\d+)\s+passed.*?(\d+)\s+total",
        re.IGNORECASE,
    )
    m = jest_pattern.search(raw_output)
    if m:
        failed = int(m.group(1))
        results.append({
            "name": "jest summary",
            "passed": failed == 0,
            "details": m.group(0),
        })
        return results

    # 通用 exit code 判断
    if "error" in raw_output.lower() or "failed" in raw_output.lower():
        results.append({
            "name": "generic",
            "passed": False,
            "details": raw_output[:500],
        })
    elif "success" in raw_output.lower() or "passed" in raw_output.lower() or "ok" in raw_output.lower():
        results.append({
            "name": "generic",
            "passed": True,
            "details": "",
        })

    return results


def extract_failure_details(raw_output: str, max_chars: int = 3000) -> str:
    """从测试输出中提取失败详情"""
    lines = raw_output.split("\n")
    failure_lines = []
    capturing = False

    for line in lines:
        lower = line.lower()
        if any(k in lower for k in ("failed", "failure", "error:", "assertion", "assert")):
            capturing = True
        if capturing:
            failure_lines.append(line)
            if len(failure_lines) > 50:
                break

    result = "\n".join(failure_lines)
    if len(result) > max_chars:
        result = result[:max_chars] + "\n... [截断]"
    return result
