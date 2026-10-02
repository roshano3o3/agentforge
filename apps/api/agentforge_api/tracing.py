"""OpenTelemetry for the API and the worker: setup, propagation, and turning
ended spans into `trace_spans` rows.

    run.create (API) -> run (worker) -> case -> invoke_agent -> planner_decision
                                                              -> retrieval
                                                              -> execute_tool <tool>
                                             -> evaluate <evaluator>

The API's span context travels to the worker inside the arq job (a W3C
traceparent carrier), and the worker's agent-call span travels to an HTTP
agent as a `traceparent` header. Spans the agent opens itself (through
agentforge_sdk.tracing, in-process python adapters only) become children of
the agent call.

Every span ends up in Postgres, written with the case (or the run) it
belongs to; an OTLP exporter is added only if OTEL_EXPORTER_OTLP_ENDPOINT
(or ..._TRACES_ENDPOINT) is set. AGENTFORGE_TRACING=off disables all of it:
no spans recorded, none stored.

Attribute names: OpenTelemetry GenAI semantic conventions where they fit
(gen_ai.operation.name, gen_ai.tool.*, gen_ai.request.model,
gen_ai.usage.*_tokens -- the latter only when the adapter reported them),
`agentforge.*` for everything else.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

from agentforge_sdk.tracing import attribute_value

TRACER_NAME = "agentforge"
_lock = threading.Lock()
_collector: SpanCollector | None = None
_enabled: bool | None = None


def enabled() -> bool:
    """AGENTFORGE_TRACING (default on); `off`, `0`, `false` disable tracing."""
    global _enabled
    if _enabled is None:
        _enabled = os.environ.get("AGENTFORGE_TRACING", "on").strip().lower() not in ("off", "0", "false", "no")
    return _enabled


class SpanCollector(SpanProcessor):
    """Keeps every ended span in memory, by trace, until the code that owns
    it (the run or one case) claims it for storage. Spans nobody claims --
    e.g. from a timed-out adapter thread that finished later -- are dropped
    with their trace."""

    def __init__(self) -> None:
        self._spans: dict[int, list[ReadableSpan]] = {}
        self._lock = threading.Lock()

    def on_end(self, span: ReadableSpan) -> None:
        ctx = span.get_span_context()
        if ctx is None:
            return
        with self._lock:
            self._spans.setdefault(ctx.trace_id, []).append(span)

    def take_subtree(self, trace_id: int, root_span_id: int) -> list[ReadableSpan]:
        """Remove and return the root span and every buffered descendant of it."""
        with self._lock:
            spans = self._spans.get(trace_id, [])
            by_id = {s.get_span_context().span_id: s for s in spans}  # type: ignore[union-attr]
            taken = []
            for s in spans:
                node: ReadableSpan | None = s
                while node is not None:
                    sid = node.get_span_context().span_id  # type: ignore[union-attr]
                    if sid == root_span_id:
                        taken.append(s)
                        break
                    parent = node.parent
                    node = by_id.get(parent.span_id) if parent is not None else None
            ids = {id(s) for s in taken}
            self._spans[trace_id] = [s for s in spans if id(s) not in ids]
            return taken

    def discard(self, trace_id: int) -> int:
        with self._lock:
            return len(self._spans.pop(trace_id, []))

    def shutdown(self) -> None:
        with self._lock:
            self._spans.clear()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def setup(service_name: str) -> SpanCollector | None:
    """Install a TracerProvider (once per process) with the span collector
    and, if configured, an OTLP exporter. None when tracing is off."""
    global _collector
    if not enabled():
        return None
    with _lock:
        if _collector is not None:
            return _collector
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        collector = SpanCollector()
        provider.add_span_processor(collector)
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            # Already set in this process (e.g. by a test harness): reuse it.
            current.add_span_processor(collector)
        else:
            trace.set_tracer_provider(provider)
        _collector = collector
        return collector


def collector() -> SpanCollector | None:
    return _collector


def tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


# -- propagation ----------------------------------------------------------------------


def inject(ctx: otel_context.Context | None = None) -> dict[str, str]:
    """The W3C trace context carrier ({"traceparent": ...}) for `ctx` (or the current one)."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier, context=ctx)
    return carrier


def extract(carrier: Mapping[str, str] | None) -> otel_context.Context | None:
    return propagate.extract(dict(carrier)) if carrier else None


# -- spans ------------------------------------------------------------------------------


def attrs(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: attribute_value(v) for k, v in values.items() if v is not None}


@contextmanager
def span(
    name: str, attributes: Mapping[str, Any] | None = None, *, context: otel_context.Context | None = None
) -> Iterator[trace.Span]:
    """A span that's current inside the block (no exception recording here:
    callers record errors themselves, with `error`)."""
    with tracer().start_as_current_span(
        name, context=context, attributes=attrs(attributes or {}), record_exception=False, set_status_on_exception=False
    ) as s:
        yield s


def error(s: trace.Span, message: str, exc: BaseException | None = None, *, event: str = "exception") -> None:
    """Status ERROR plus an event describing what went wrong."""
    if exc is not None:
        s.record_exception(exc)
    else:
        s.add_event(event, {"exception.message": message})
    s.set_status(Status(StatusCode.ERROR, message))


def hex_span_id(s: trace.Span | ReadableSpan) -> str:
    return format(s.get_span_context().span_id, "016x")  # type: ignore[union-attr]


def hex_trace_id(s: trace.Span | ReadableSpan) -> str:
    return format(s.get_span_context().trace_id, "032x")  # type: ignore[union-attr]


def _time(ns: int | None) -> datetime:
    return datetime.fromtimestamp((ns or 0) / 1e9, tz=UTC)


def row_values(s: ReadableSpan) -> dict[str, Any]:
    """The columns of a `trace_spans` row for an ended span."""
    ctx = s.get_span_context()
    assert ctx is not None
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_span_id": format(s.parent.span_id, "016x") if s.parent is not None else None,
        "name": s.name,
        "kind": s.kind.name.lower(),
        "service": str(s.resource.attributes.get("service.name", "")),
        "start_time": _time(s.start_time),
        "end_time": _time(s.end_time),
        "duration_ms": ((s.end_time or 0) - (s.start_time or 0)) / 1e6,
        "attributes": dict(s.attributes or {}),
        "status_code": s.status.status_code.name,
        "status_message": s.status.description,
        "events": [
            {"name": e.name, "time": _time(e.timestamp).isoformat(), "attributes": dict(e.attributes or {})}
            for e in s.events
        ],
    }
