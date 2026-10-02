"""Optional OpenTelemetry spans from inside an agent.

An agent that wants its own work in the AgentForge trace -- each planner
decision, retrieval and tool call, timed by the agent itself -- opens spans
with these helpers. They use the OpenTelemetry API if it's installed
(`pip install agentforge-sdk[tracing]`) and are no-ops if it isn't: nothing to
configure, nothing raised, `span_id` is None.

When a python adapter runs inside the AgentForge worker, the worker's span
for the agent call is the current span, so these become its children and are
stored with the case. An HTTP agent receives a W3C `traceparent` header
instead; continuing the trace there is up to the agent's own tracer.

AgentForge never creates these spans on an agent's behalf: steps an agent
reports without spans stay without spans.

    from agentforge_sdk import tracing

    with tracing.span("execute_tool get_invoice", {"gen_ai.tool.name": "get_invoice"}) as s:
        result = get_invoice(...)
        s.set("agentforge.tool.result", result)
    step = Step(kind="tool_call", ..., span_id=s.span_id)

No server, database or framework imports here: any application can use it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

try:  # pragma: no cover - which branch runs depends on the environment
    from opentelemetry import context as _otel_context
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # OTel not installed: every helper below is a no-op
    _otel_trace = None  # type: ignore[assignment]
    _otel_context = None  # type: ignore[assignment]

TRACER_NAME = "agentforge.agent"
# Attribute values longer than this are cut, with a marker, so one huge tool
# result can't bloat a trace.
MAX_ATTRIBUTE_CHARS = 16_000


def available() -> bool:
    """True if the OpenTelemetry API is installed (spans may be recorded)."""
    return _otel_trace is not None


def attribute_value(value: Any) -> str | int | float | bool:
    """An OTel-compatible attribute value: scalars as-is, anything else as
    compact JSON, cut at MAX_ATTRIBUTE_CHARS."""
    if isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    if len(text) > MAX_ATTRIBUTE_CHARS:
        text = text[:MAX_ATTRIBUTE_CHARS] + "…(truncated)"
    return text


class SpanHandle:
    """What `span()` yields. Safe to use whether or not OTel is installed."""

    def __init__(self, span: Any = None) -> None:
        self._span = span

    @property
    def span_id(self) -> str | None:
        """16 lowercase hex characters, or None when not recording."""
        if self._span is None:
            return None
        ctx = self._span.get_span_context()
        return format(ctx.span_id, "016x") if ctx.is_valid else None

    @property
    def context(self) -> Any:
        """An OTel Context with this span current (for parenting a later span), or None."""
        if self._span is None or _otel_trace is None:
            return None
        return _otel_trace.set_span_in_context(self._span)

    def set(self, key: str, value: Any) -> None:
        if self._span is not None and value is not None:
            self._span.set_attribute(key, attribute_value(value))

    def event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        if self._span is not None:
            self._span.add_event(name, {k: attribute_value(v) for k, v in (attributes or {}).items()})

    def error(self, message: str, exc: BaseException | None = None) -> None:
        """Mark the span failed: status ERROR plus an event (the exception, if given)."""
        if self._span is None:
            return
        if exc is not None:
            self._span.record_exception(exc)
        else:
            self._span.add_event("error", {"message": message})
        self._span.set_status(Status(StatusCode.ERROR, message))


@contextmanager
def span(name: str, attributes: Mapping[str, Any] | None = None, *, parent: Any = None) -> Iterator[SpanHandle]:
    """Open a span (a child of the current one, or of `parent`, a
    SpanHandle.context). An exception escaping the block marks it ERROR and
    is re-raised."""
    if _otel_trace is None:
        yield SpanHandle()
        return
    tracer = _otel_trace.get_tracer(TRACER_NAME)
    attrs = {k: attribute_value(v) for k, v in (attributes or {}).items() if v is not None}
    with tracer.start_as_current_span(
        name, context=parent, attributes=attrs, record_exception=True, set_status_on_exception=True
    ) as otel_span:
        yield SpanHandle(otel_span)


def annotate(key: str, value: Any) -> None:
    """Set an attribute on the current span, if one is recording."""
    if _otel_trace is None or value is None:
        return
    current = _otel_trace.get_current_span()
    if current.is_recording():
        current.set_attribute(key, attribute_value(value))
