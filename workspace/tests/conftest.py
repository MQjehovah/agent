#!/usr/bin/env python3
"""
Pytest conftest.py — 提供 tr (TestResult) fixture 给所有测试用例
"""

import pytest


class TestResult:
    """测试结果记录器"""
    def __init__(self):
        self.results = []
        self.passed = 0
        self.failed = 0

    def record(self, test_id, name, passed, detail=""):
        status = "PASS" if passed else "FAIL"
        self.results.append({
            "id": test_id,
            "name": name,
            "status": status,
            "detail": detail
        })
        if passed:
            self.passed += 1
        else:
            self.failed += 1
        symbol = "✓" if passed else "✗"
        print(f"  [{symbol}] {test_id}: {name}")
        if detail:
            print(f"      Detail: {detail}")

    def summary(self):
        total = self.passed + self.failed
        print(f"\n{'='*60}")
        print(f"  测试结果: {self.passed}/{total} PASSED, {self.failed} FAILED")
        print(f"{'='*60}")
        return self.failed == 0


@pytest.fixture
def tr():
    """提供 TestResult 实例作为 fixture"""
    return TestResult()
