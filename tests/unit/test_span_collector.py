"""The worker's in-memory span collector: a case claims exactly its own
subtree, and spans nobody claims (e.g. from a hung adapter thread) are
discarded with the trace instead of being stored under the wrong case."""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import set_span_in_context

from agentforge_api.tracing import SpanCollector, row_values
from agentforge_sdk.tracing import MAX_ATTRIBUTE_CHARS, attribute_value


def test_take_subtree_returns_the_root_and_its_descendants_only() -> None:
    collector = SpanCollector()
    provider = TracerProvider()
    provider.add_span_processor(collector)
    tracer = provider.get_tracer("t")

    run = tracer.start_span("run")
    run_ctx = set_span_in_context(run)
    case_a = tracer.start_span("case a", context=run_ctx)
    child = tracer.start_span("tool", context=set_span_in_context(case_a))
    grandchild = tracer.start_span("nested", context=set_span_in_context(child))
    for s in (grandchild, child, case_a):
        s.end()
    case_b = tracer.start_span("case b", context=run_ctx)
    case_b.end()

    a = case_a.get_span_context()
    taken = collector.take_subtree(a.trace_id, a.span_id)
    assert sorted(s.name for s in taken) == ["case a", "nested", "tool"]
    # Claimed spans are gone; the other case's span is still buffered.
    assert collector.take_subtree(a.trace_id, a.span_id) == []
    b = case_b.get_span_context()
    assert [s.name for s in collector.take_subtree(b.trace_id, b.span_id)] == ["case b"]

    # A straggler that ends after its case was claimed is dropped with the trace.
    late = tracer.start_span("late", context=set_span_in_context(case_a))
    late.end()
    run.end()
    r = run.get_span_context()
    # Its parent (case a) was already claimed, so it no longer descends from the run: not stored...
    assert [s.name for s in collector.take_subtree(r.trace_id, r.span_id)] == ["run"]
    # ...and the worker discards what's left of the trace when the run ends.
    assert collector.discard(r.trace_id) == 1

    row = row_values(taken[0])
    assert len(row["trace_id"]) == 32 and len(row["span_id"]) == 16
    assert row["status_code"] == "UNSET" and row["duration_ms"] >= 0


def test_attribute_values_are_scalars_or_compact_json_and_capped() -> None:
    assert attribute_value(3) == 3
    assert attribute_value({"b": 1, "a": [1, 2]}) == '{"a": [1, 2], "b": 1}'
    long = attribute_value("x" * (MAX_ATTRIBUTE_CHARS + 10))
    assert isinstance(long, str) and long.endswith("…(truncated)")
    assert len(long) == MAX_ATTRIBUTE_CHARS + len("…(truncated)")
