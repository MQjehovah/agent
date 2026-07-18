"""
错误分类与分级恢复系统 — 按错误类型精准恢复

设计思路：
- 不是所有错误都该用相同策略重试
- 语法错误 → 立即重试（参数问题）
- 网络错误 → 退避重试（临时故障）
- 权限错误 → 不重试（环境问题）
- 逻辑错误 → 换方案（思路问题）
- 超时错误 → 限流重试（负载问题）

用法:
    classifier = ErrorClassifier()
    error_type = classifier.classify(name, args, exception)
    strategy = classifier.get_recovery(error_type, attempt)
    # strategy = {"action": "retry|refactor|escalate|skip", "delay": 2.0, "advice": "..."}
"""
import enum
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("agent.error_classifier")


class ErrorType(enum.Enum):
    """错误类型分类"""
    SYNTAX = "syntax"             # 参数错误、格式错误
    NETWORK = "network"           # 连接超时、DNS 失败
    TIMEOUT = "timeout"           # 执行超时
    PERMISSION = "permission"     # 权限不足、文件只读
    NOT_FOUND = "not_found"       # 文件不存在、命令未找到
    DEPENDENCY = "dependency"     # 依赖缺失、版本冲突
    LOGIC = "logic"              # 业务逻辑错误
    RESOURCE = "resource"         # 内存不足、磁盘满
    RATE_LIMIT = "rate_limit"     # API 限流
    UNKNOWN = "unknown"           # 未分类


@dataclass
class RecoveryStrategy:
    """恢复策略"""
    action: str                    # retry / refactor / escalate / skip / wait
    delay: float = 0.0             # 重试前等待秒数
    max_retries: int = 3           # 最大重试次数
    advice: str = ""               # 给 LLM 的建议
    requires_rewrite: bool = False  # 是否需要改思路而不是重试


class ErrorClassifier:
    """错误分类器 — 根据工具名、参数、异常分类错误"""

    # 类型 -> 关键词模式
    PATTERNS: dict[ErrorType, list[str]] = {
        ErrorType.SYNTAX: [
            "invalid syntax", "parse error", "unexpected token",
            "missing", "unexpected", "bad argument",
            "语法错误", "格式错误",
        ],
        ErrorType.NETWORK: [
            "connection refused", "connection reset", "connection timed out",
            "network is unreachable", "dns lookup failed",
            "连接拒绝", "网络超时", "网络不可达",
        ],
        ErrorType.TIMEOUT: [
            "timeout", "timed out", "超时",
            "killed", " hangs",
        ],
        ErrorType.PERMISSION: [
            "permission denied", "access denied", "not allowed",
            "read-only", "不允许", "权限不足", "拒绝访问",
        ],
        ErrorType.NOT_FOUND: [
            "no such file", "not found", "command not found",
            "找不到", "不存在", "未找到",
        ],
        ErrorType.DEPENDENCY: [
            "module not found", "cannot import", "no module named",
            "missing required", "dependency",
            "未安装", "缺少依赖", "导入失败",
        ],
        ErrorType.RESOURCE: [
            "out of memory", "disk full", "no space",
            "内存不足", "磁盘空间", "资源不足",
        ],
        ErrorType.RATE_LIMIT: [
            "rate limit", "too many requests", "429",
            "请求过多", "频率限制",
        ],
    }

    # 工具级错误映射（某些工具的错误几乎总是特定类型）
    TOOL_ERROR_MAP: dict[str, ErrorType] = {
        "file_operation": ErrorType.NOT_FOUND,      # 默认为文件未找到
        "edit": ErrorType.SYNTAX,                    # 默认为替换文本不匹配
        "shell": ErrorType.TIMEOUT,                  # 默认为命令超时
        "web_search": ErrorType.NETWORK,             # 默认为网络问题
        "web_fetch": ErrorType.NETWORK,              # 默认为网络问题
        "code_search": ErrorType.NOT_FOUND,          # 默认为符号未找到
        "batch_edit": ErrorType.SYNTAX,              # 默认为锚点不匹配
    }

    def __init__(self):
        self._history: list[dict] = []

    def classify(
        self,
        tool_name: str,
        args: Optional[dict],
        exception: Exception,
    ) -> ErrorType:
        """对错误进行分类

        Args:
            tool_name: 工具名
            args: 工具参数
            exception: 异常对象

        Returns:
            分类结果
        """
        error_msg = str(exception).lower()

        # 1. 精确匹配模式
        for error_type, patterns in self.PATTERNS.items():
            for pattern in patterns:
                if pattern.lower() in error_msg:
                    self._log(tool_name, error_type)
                    return error_type

        # 2. 工具默认映射
        if tool_name in self.TOOL_ERROR_MAP:
            base_type = self.TOOL_ERROR_MAP[tool_name]
            self._log(tool_name, base_type)
            return base_type

        # 3. 启发式判断
        if isinstance(exception, TimeoutError):
            return ErrorType.TIMEOUT
        if isinstance(exception, PermissionError):
            return ErrorType.PERMISSION
        if isinstance(exception, FileNotFoundError):
            return ErrorType.NOT_FOUND
        if isinstance(exception, ConnectionError):
            return ErrorType.NETWORK

        # 4. 根据工具参数启发式
        if tool_name in ("edit", "batch_edit") and args:
            if "old" in args and args["old"] not in self._get_file_content(args.get("file", "")):
                return ErrorType.SYNTAX

        return ErrorType.UNKNOWN

    def get_recovery(
        self,
        error_type: ErrorType,
        attempt: int = 0,
        tool_name: str = "",
    ) -> RecoveryStrategy:
        """根据错误类型和尝试次数返回恢复策略

        Args:
            error_type: 错误类型
            attempt: 已尝试次数（0-based）
            tool_name: 工具名（用于特定工具的恢复建议）

        Returns:
            恢复策略
        """
        strategies = {
            ErrorType.SYNTAX: RecoveryStrategy(
                action="refactor",
                delay=0.0,
                max_retries=1,
                advice="检查参数和输入格式是否正确，重新构造请求",
                requires_rewrite=True,
            ),
            ErrorType.NETWORK: RecoveryStrategy(
                action="retry",
                delay=min(2.0 * (2 ** attempt), 30.0),
                max_retries=3,
                advice="网络临时故障，等待后重试",
            ),
            ErrorType.TIMEOUT: RecoveryStrategy(
                action="retry",
                delay=min(1.0 * (2 ** attempt), 15.0),
                max_retries=2,
                advice="操作超时，可能是数据量太大，尝试缩小操作范围",
                requires_rewrite=(attempt >= 2),
            ),
            ErrorType.PERMISSION: RecoveryStrategy(
                action="escalate",
                delay=0.0,
                max_retries=0,
                advice="权限不足，无法执行此操作，报告给用户",
                requires_rewrite=True,
            ),
            ErrorType.NOT_FOUND: RecoveryStrategy(
                action="refactor",
                delay=0.0,
                max_retries=1,
                advice="文件或符号不存在，检查路径或先创建文件",
                requires_rewrite=True,
            ),
            ErrorType.DEPENDENCY: RecoveryStrategy(
                action="skip",
                delay=0.0,
                max_retries=1,
                advice="缺少依赖，先通过 shell 安装依赖后再重试",
            ),
            ErrorType.RESOURCE: RecoveryStrategy(
                action="escalate",
                delay=10.0,
                max_retries=1,
                advice="资源不足，等待后重试或减少操作规模",
                requires_rewrite=(attempt >= 1),
            ),
            ErrorType.RATE_LIMIT: RecoveryStrategy(
                action="wait",
                delay=min(5.0 * (2 ** attempt), 60.0),
                max_retries=5,
                advice="API 限流，等待后重试",
            ),
            ErrorType.UNKNOWN: RecoveryStrategy(
                action="retry",
                delay=1.0,
                max_retries=2,
                advice="未知错误，检查参数和环境后重试",
                requires_rewrite=(attempt >= 2),
            ),
        }

        strategy = strategies.get(error_type, strategies[ErrorType.UNKNOWN])

        # 工具特定的恢复建议
        tool_advice = self._get_tool_advice(tool_name, error_type)
        if tool_advice:
            strategy.advice = tool_advice

        return strategy

    def get_error_summary(self) -> dict:
        """获取错误统计"""
        if not self._history:
            return {"total": 0, "by_type": {}, "tools": []}

        by_type: dict[str, int] = {}
        tools: set[str] = set()
        for h in self._history:
            t = h["type"].value if hasattr(h["type"], 'value') else str(h["type"])
            by_type[t] = by_type.get(t, 0) + 1
            tools.add(h["tool"])

        return {
            "total": len(self._history),
            "by_type": dict(sorted(by_type.items(), key=lambda x: -x[1])),
            "tools": sorted(tools),
        }

    def _get_tool_advice(self, tool_name: str, error_type: ErrorType) -> str:
        """工具特定的恢复建议"""
        advice_map = {
            ("edit", ErrorType.SYNTAX): "原文可能已被修改，重新读取文件后再提交编辑",
            ("edit", ErrorType.NOT_FOUND): "文件可能已被移动或删除，先确认文件路径",
            ("batch_edit", ErrorType.SYNTAX): "某个锚点不匹配或原文不唯一，逐个检查每个编辑项",
            ("batch_edit", ErrorType.NOT_FOUND): "文件列表中某个文件不存在，先确认所有文件路径",
            ("shell", ErrorType.TIMEOUT): "命令执行太久，先检查命令是否正确，或拆分为小步骤执行",
            ("shell", ErrorType.NOT_FOUND): "命令未找到，先检查是否安装了对应工具",
            ("code_search", ErrorType.NOT_FOUND): "符号未找到，尝试不同的大小写或缩写",
            ("file_operation", ErrorType.PERMISSION): "文件受保护，尝试其他路径或使用 shell 检查权限",
        }
        return advice_map.get((tool_name, error_type), "")

    @staticmethod
    def _get_file_content(file_path: str) -> str:
        """获取文件内容（用于验证 edit 锚点）"""
        if not file_path or not os.path.isfile(file_path):
            return ""
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""

    def _log(self, tool_name: str, error_type: ErrorType):
        """记录错误"""
        self._history.append({
            "tool": tool_name,
            "type": error_type,
            "time": time.time(),
        })


# 兼容旧接口
import os  # noqa: E402
