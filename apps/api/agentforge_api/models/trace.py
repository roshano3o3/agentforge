from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow


class TraceSpan(Base):
    """One OpenTelemetry span recorded for a run, stored as it ended.

    Run-level spans (the API's `agentforge.run.create`, the worker's
    `agentforge.run`) have no `evaluation_result_id`; a case's spans (its
    `agentforge.case` span and everything under it: the agent call, the
    agent's own planner/retrieval/tool spans, one per evaluator) point to that
    case's result. Written only while the run is pending or running, then
    frozen with it (DB triggers in the `trace_spans` migration).
    """

    __tablename__ = "trace_spans"
    __table_args__ = (
        UniqueConstraint("trace_id", "span_id", name="uq_trace_span"),
        CheckConstraint("status_code IN ('UNSET', 'OK', 'ERROR')", name="ck_trace_span_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_runs.id"), nullable=False, index=True)
    evaluation_result_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("evaluation_results.id"), nullable=True, index=True
    )
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # Which process recorded it ("agentforge-api", "agentforge-worker").
    service: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # UNSET | OK | ERROR
    status_code: Mapped[str] = mapped_column(String(10), nullable=False)
    status_message: Mapped[str | None] = mapped_column(String, nullable=True)
    # [{name, time, attributes}], e.g. a recorded exception.
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
