"""Per-invocation bookkeeping that links the agent's spans to its steps.

One agent run (one test case) sets a Recorder in a context variable; the
planner and the traced tools read it. A context variable, not a global:
the graph is shared across concurrent invocations, and LangGraph runs nodes
and tools in threads that inherit a copy of the caller's context (which is
also how the worker's `invoke_agent` span becomes their parent).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from agentforge_sdk.tracing import SpanHandle


@dataclass
class Recorder:
    # The latest planner decision, so the tool call it chose is its child.
    decision: SpanHandle | None = None
    decision_call_id: str | None = None
    # One per executed tool call, in order (None when not tracing).
    tool_span_ids: list[str | None] = field(default_factory=list)
    # The decision that produced the final answer.
    final_span_id: str | None = None


_current: ContextVar[Recorder | None] = ContextVar("invoice_agent_recorder", default=None)


def current_recorder() -> Recorder | None:
    return _current.get()


@contextmanager
def recording() -> Iterator[Recorder]:
    recorder = Recorder()
    token = _current.set(recorder)
    try:
        yield recorder
    finally:
        _current.reset(token)
