"""Stored span rows -> the nested tree the API returns (GET /traces, GET /replays)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from agentforge_core.schemas import TraceSpanOut


def span_tree(rows: Iterable[Any], step_by_span_id: Mapping[str, int]) -> list[TraceSpanOut]:
    """`rows`: TraceSpan or ReplaySpan rows (same columns). Each span is put
    under its parent when the parent is among `rows`, else it's a root;
    siblings are ordered by start time. `step_by_span_id` marks the agent step
    that reported each span."""
    nodes = {r.span_id: _node(r, step_by_span_id.get(r.span_id)) for r in rows}
    roots: list[TraceSpanOut] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_span_id or "")
        (parent.children if parent is not None else roots).append(node)
    for node in nodes.values():
        node.children.sort(key=lambda n: (n.start_time, n.name))
    roots.sort(key=lambda n: n.start_time)
    return roots


def _node(s: Any, step_index: int | None) -> TraceSpanOut:
    return TraceSpanOut(
        span_id=s.span_id,
        parent_span_id=s.parent_span_id,
        name=s.name,
        kind=s.kind,
        service=s.service,
        start_time=s.start_time,
        end_time=s.end_time,
        duration_ms=s.duration_ms,
        attributes=s.attributes,
        status_code=s.status_code,
        status_message=s.status_message,
        events=s.events,
        step_index=step_index,
        children=[],
    )
