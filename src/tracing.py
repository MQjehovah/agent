"""
调用链追踪系统 — 支持父子 Span 关联、JSONL 导出、跨 Agent 追踪。

设计思路（参考 OpenTelemetry）：
- Trace = 一次完整请求的调用链
- Span = Trace 中的一个操作单元
- Span 通过 trace_id + parent_id 形成树状结构
- 导出 JSONL 格式，可被 Jaeger / Grafana Tempo 等工具消费

用法:
    tracer = Tracer()
    tracer.start_trace("agent.run: refactor user service")
    tracer.start_span("subagent:代码工程师")
    # ... 干活 ...
    tracer.end_span()
    tracer.start_span("subagent:测试工程师")
    # ... 干活 ...
    tracer.end_span()
    tracer.end_trace()
"""
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("agent.trace")


@dataclass
class Span:
    """调用链中的一个 Span

    Attributes:
        trace_id: 所属 Trace 的 ID
        span_id: 本 Span 的 ID
        parent_id: 父 Span 的 ID（空字符串表示根 Span）
        operation: 操作名称
        start_time: 开始时间戳
        end_time: 结束时间戳（None 表示未完成）
        status: "ok" / "error" / "cancelled"
        attributes: 自定义属性
        context_tokens: 上下文 token 数
        agent_id: 关联的 Agent ID
        agent_role: 关联的 Agent 角色
        tool_calls: 调用的工具列表
        model: 使用的模型名
    """
    trace_id: str
    span_id: str
    parent_id: str
    operation: str
    start_time: float = field(default_factory=time.time)
    end_time: float = None
    status: str = "ok"
    attributes: dict = field(default_factory=dict)
    context_tokens: int = 0
    agent_id: str = ""
    agent_role: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""

    @property
    def duration_ms(self) -> float:
        return ((self.end_time or time.time()) - self.start_time) * 1000

    @property
    def is_root(self) -> bool:
        return not self.parent_id

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "operation": self.operation,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration_ms": round(self.duration_ms, 2),
            "status": self.status,
            "context_tokens": self.context_tokens,
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "tool_calls": len(self.tool_calls),
            "model": self.model,
            "attributes": {k: v for k, v in self.attributes.items()
                          if isinstance(v, (str, int, float, bool))},
        }


class JSONLExporter:
    """把完成的 Span 以 JSON Lines 写入文件

    每行一个 span 记录，便于:
    - jq / grep 命令行分析
    - 导入 Jaeger / Grafana Tempo
    - 自定义看板消费
    """

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._file = None

    def export(self, span: Span):
        try:
            record = span.to_dict()
            line = json.dumps(record, ensure_ascii=False) + "\n"
            if self._file is None:
                self._file = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
            self._file.write(line)
            self._file.flush()
        except Exception as ex:
            logger.warning(f"[trace] JSONL 导出失败: {ex}")

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


class Tracer:
    """调用链追踪器

    线程安全说明：假设在单线程 asyncio 中使用。
    每个 Agent 实例拥有自己的 Tracer，互不干扰。
    """

    def __init__(self, exporters: list = None):
        # Stack 模型：_spans[-1] 是当前活跃 Span
        self._spans: list[Span] = []
        self._context_history: list[dict] = []
        # 已完成 span 的完整列表（用于统计和导出）
        self._completed_spans: list[Span] = []
        self._exporters = exporters if exporters is not None else self._default_exporters()

    @staticmethod
    def _default_exporters() -> list:
        try:
            from settings import get_settings
            jsonl_path = get_settings().get("observability.jsonl_path", "")
        except RuntimeError:
            return []
        if jsonl_path:
            return [JSONLExporter(jsonl_path)]
        return []

    # ── Trace 生命周期 ─────────────────────────────────

    def start_trace(self, operation: str, attributes: dict = None) -> str:
        """开始一个新的 Trace（根 Span）"""
        tid = str(uuid.uuid4())[:12]
        span = Span(
            trace_id=tid,
            span_id=str(uuid.uuid4())[:8],
            parent_id="",
            operation=operation,
            attributes=attributes or {},
        )
        self._spans.append(span)
        logger.debug(f"[{tid}] 开始 trace: {operation}")
        return tid

    def start_span(
        self,
        operation: str,
        attributes: dict = None,
        agent_id: str = "",
        agent_role: str = "",
    ) -> str:
        """开始一个新的 Span（自动关联当前活跃 Span 为父）

        Args:
            operation: 操作名，如 "subagent:代码工程师", "tool:shell"
            attributes: 自定义属性
            agent_id: Agent ID
            agent_role: Agent 角色

        Returns:
            span_id
        """
        if not self._spans:
            self.start_trace(operation)
            return self._spans[-1].span_id

        parent = self._spans[-1]
        sid = str(uuid.uuid4())[:8]
        span = Span(
            trace_id=parent.trace_id,
            span_id=sid,
            parent_id=parent.span_id,
            operation=operation,
            attributes=attributes or {},
            agent_id=agent_id or parent.agent_id,
            agent_role=agent_role or parent.agent_role,
        )
        self._spans.append(span)
        logger.debug(f"[{parent.trace_id}] 开始 {operation} (span={sid})")
        return sid

    def end_span(self, status: str = "ok", **attrs):
        """结束当前 Span

        Args:
            status: "ok" / "error" / "cancelled"
            **attrs: 合并到 attributes 中的额外属性
        """
        if not self._spans:
            return
        span = self._spans.pop()
        span.end_time = time.time()
        span.status = status
        span.attributes.update(attrs)
        # 记录到已完成列表
        self._completed_spans.append(span)
        # 导出
        for exporter in self._exporters:
            try:
                exporter.export(span)
            except Exception as ex:
                logger.warning(f"[trace] 导出失败: {ex}")
        logger.debug(
            f"[{span.trace_id}] 结束 {span.operation} "
            f"({span.duration_ms:.0f}ms, {status}"
            + (f", ctx={span.context_tokens}token" if span.context_tokens else "")
            + ")"
        )

    def end_trace(self, status: str = "ok"):
        """结束整个 Trace（从根到当前全部关闭）"""
        while self._spans:
            self.end_span(status)

    # ── Span 属性修改 ──────────────────────────────────

    def record_context_size(self, token_count: int):
        """记录当前上下文 token 数到活跃 span"""
        if self._spans:
            self._spans[-1].context_tokens = token_count
        self._context_history.append({
            "tokens": token_count,
            "time": time.time(),
            "operation": self._spans[-1].operation if self._spans else "",
        })

    def record_tool_call(self, tool_name: str, args: dict = None, result: str = None):
        """记录工具调用到当前活跃 span"""
        if self._spans:
            self._spans[-1].tool_calls.append({
                "name": tool_name,
                "args": str(args)[:200] if args else "",
                "result_preview": str(result)[:200] if result else "",
            })

    def set_agent_info(self, agent_id: str, agent_role: str = ""):
        """设置当前活跃 span 的 Agent 信息"""
        if self._spans:
            self._spans[-1].agent_id = agent_id
            self._spans[-1].agent_role = agent_role or self._spans[-1].agent_role

    def set_model(self, model: str):
        if self._spans:
            self._spans[-1].model = model

    # ── 查询接口 ──────────────────────────────────────

    def get_context_stats(self) -> dict:
        """获取上下文大小统计"""
        if not self._context_history:
            return {"samples": 0, "peak": 0, "final": 0, "avg": 0}
        tokens = [h["tokens"] for h in self._context_history]
        return {
            "samples": len(self._context_history),
            "peak": max(tokens),
            "final": tokens[-1],
            "avg": int(sum(tokens) / len(tokens)),
        }

    def get_active_trace_id(self) -> str:
        return self._spans[0].trace_id if self._spans else ""

    def get_span_tree(self) -> list[dict]:
        """获取完整的 Span 树（用于 TUI/Web 展示）"""
        # 按 parent_id 分组
        children_of: dict[str, list[Span]] = defaultdict(list)
        all_spans = self._spans + self._completed_spans
        for s in all_spans:
            children_of[s.parent_id].append(s)

        def _build(node_id: str) -> list[dict]:
            result = []
            for s in children_of.get(node_id, []):
                subtree = {
                    "span_id": s.span_id,
                    "operation": s.operation,
                    "status": s.status,
                    "duration_ms": round(s.duration_ms, 1),
                    "context_tokens": s.context_tokens,
                    "agent_role": s.agent_role,
                    "children": _build(s.span_id),
                }
                result.append(subtree)
            return result

        roots = [s for s in all_spans if s.is_root]
        tree = []
        for r in roots:
            tree.append({
                "trace_id": r.trace_id,
                "operation": r.operation,
                "status": r.status,
                "duration_ms": round(r.duration_ms, 1),
                "agent_role": r.agent_role,
                "children": _build(r.span_id),
                "is_root": True,
            })
        return tree

    def get_trace_summary(self) -> dict:
        """获取当前 Trace 的汇总数据"""
        all_spans = self._spans + self._completed_spans
        if not all_spans:
            return {}

        root = all_spans[0]
        subagent_spans = [s for s in all_spans if "subagent" in s.operation or "团队" in s.operation]
        tool_spans = [s for s in all_spans if s.operation.startswith("tool.")]
        return {
            "trace_id": root.trace_id,
            "operation": root.operation,
            "total_duration_ms": round(root.duration_ms, 1),
            "total_spans": len(all_spans),
            "subagent_count": len(subagent_spans),
            "tool_call_count": sum(len(s.tool_calls) for s in all_spans),
            "peak_context_tokens": self.get_context_stats().get("peak", 0),
            "status": root.status,
        }

    @property
    def has_active_span(self) -> bool:
        return len(self._spans) > 0

    @property
    def active_operation(self) -> str:
        return self._spans[-1].operation if self._spans else ""
