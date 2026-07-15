"""P4b 测试：可观测性导出（Span → JSONL）。

验证 JSONLExporter 写 span 记录、Tracer 在 end_span 时导出完成的 span、
默认无 exporter 保持原内存行为、以及从 settings.observability.jsonl_path 自动启用。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from tracing import JSONLExporter, Span, Tracer  # noqa: E402


def test_jsonl_exporter_writes_span(tmp_path):
    path = str(tmp_path / "trace.jsonl")
    exp = JSONLExporter(path)
    span = Span(trace_id="t1", span_id="s1", parent_id="", operation="agent.run")
    span.end_time = span.start_time + 0.5
    exp.export(span)
    with open(path, encoding="utf-8") as f:
        rec = json.loads(f.readline())
    assert rec["trace_id"] == "t1"
    assert rec["operation"] == "agent.run"
    assert rec["duration_ms"] >= 0


def test_tracer_exports_completed_span(tmp_path):
    path = str(tmp_path / "trace.jsonl")
    tracer = Tracer(exporters=[JSONLExporter(path)])
    tracer.start_trace("agent.run")
    tracer.start_span("tool.shell")
    tracer.end_span(status="ok")
    tracer.end_span(status="ok")
    with open(path, encoding="utf-8") as f:
        ops = [json.loads(line)["operation"] for line in f if line.strip()]
    assert "tool.shell" in ops
    assert "agent.run" in ops


def test_tracer_no_exporter_by_default():
    """默认 Tracer() 无 exporter（settings 未配 jsonl_path），end_span 不报错。"""
    tracer = Tracer()
    assert tracer._exporters == []
    tracer.start_trace("op")
    tracer.end_span()  # 不抛


def test_tracer_picks_exporter_from_settings(tmp_path):
    from settings import init_settings
    cfg = tmp_path / "config.json"
    path = str(tmp_path / "trace.jsonl")
    cfg.write_text(json.dumps({"observability": {"jsonl_path": path}}), encoding="utf-8")
    init_settings(str(tmp_path))
    tracer = Tracer()
    assert len(tracer._exporters) == 1
    assert isinstance(tracer._exporters[0], JSONLExporter)
    tracer.start_trace("op")
    tracer.end_span()
    assert os.path.exists(path)
