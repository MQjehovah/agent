import uuid
import time
import json
import logging
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

    @property
    def duration_ms(self) -> float:
        return ((self.end_time or time.time()) - self.start_time) * 1000


class Tracer:
    def __init__(self):
        self._spans: list = []

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
        logger.debug(
            f"[{span.trace_id}] 结束 {span.operation} "
            f"({span.duration_ms:.0f}ms, {status})"
        )

    def get_active_trace_id(self) -> str:
        return self._spans[0].trace_id if self._spans else ""

    @property
    def has_active_span(self) -> bool:
        return len(self._spans) > 0
