import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("agent.trace")


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    operation: str
    start_time: float = field(default_factory=time.time)
    end_time: float = None
    status: str = "ok"
    attributes: dict = field(default_factory=dict)
    context_tokens: int = 0

    @property
    def duration_ms(self) -> float:
        return ((self.end_time or time.time()) - self.start_time) * 1000


class JSONLExporter:
    """把完成的 Span 以 JSON Lines 写入文件（OTel 风格，无重依赖）。

    每行一个 span 记录，便于用 jq / grep / 外部看板消费。
    启用方式：在 config.json 设置 observability.jsonl_path。
    """

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def export(self, span: Span):
        record = {
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_id": span.parent_id,
            "operation": span.operation,
            "start_time": span.start_time,
            "end_time": span.end_time,
            "duration_ms": round(span.duration_ms, 2),
            "status": span.status,
            "context_tokens": span.context_tokens,
            "attributes": span.attributes,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class Tracer:
    def __init__(self, exporters=None):
        self._spans: list = []
        self._context_history: list = []
        self._exporters = exporters if exporters is not None else self._default_exporters()

    @staticmethod
    def _default_exporters() -> list:
        """从 settings 读取导出配置（未配置则无导出，保持原内存行为）。"""
        try:
            from settings import get_settings
            jsonl_path = get_settings().get("observability.jsonl_path", "")
        except RuntimeError:
            return []
        if jsonl_path:
            return [JSONLExporter(jsonl_path)]
        return []

    def start_trace(self, operation: str) -> str:
        tid = str(uuid.uuid4())[:12]
        span = Span(
            trace_id=tid,
            span_id=str(uuid.uuid4())[:8],
            parent_id="",
            operation=operation,
        )
        self._spans.append(span)
        logger.debug(f"[{tid}] 开始 trace: {operation}")
        return tid

    def start_span(self, operation: str) -> str:
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
        )
        self._spans.append(span)
        logger.debug(f"[{parent.trace_id}] 开始 {operation} (span={sid})")
        return sid

    def end_span(self, status: str = "ok", **attrs):
        if not self._spans:
            return
        span = self._spans.pop()
        span.end_time = time.time()
        span.status = status
        span.attributes.update(attrs)
        for exporter in self._exporters:
            try:
                exporter.export(span)
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"[trace] 导出失败: {ex}")
        logger.debug(
            f"[{span.trace_id}] 结束 {span.operation} "
            f"({span.duration_ms:.0f}ms, {status}"
            + (f", ctx={span.context_tokens}token" if span.context_tokens else "")
            + ")"
        )

    def record_context_size(self, token_count: int):
        """记录当前上下文 token 数到活跃 span"""
        if self._spans:
            self._spans[-1].context_tokens = token_count
        self._context_history.append({
            "tokens": token_count,
            "time": time.time(),
            "operation": self._spans[-1].operation if self._spans else "",
        })

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

    @property
    def has_active_span(self) -> bool:
        return len(self._spans) > 0
