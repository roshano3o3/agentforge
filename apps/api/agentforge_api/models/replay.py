"""Failure replay: one evaluated case re-run, possibly with overrides.

A replay copies everything that defines the original case's evaluation --
the run's adapter, pinned evaluator versions, threshold, latency budget and
timeout; the case (same dataset version, so the same input and scenario) --
and changes only what its `overrides` say. Its outcome lives in its own
tables (`replays`, `replay_steps`, `replay_metric_scores`, `replay_spans`),
shaped like a run's result, steps, scores and spans, so the original run is
never touched.

pending -> running -> completed | failed, like a run. Only the worker writes
the outcome; once completed or failed the replay and everything under it is
frozen (service layer + DB triggers, migration `d4a8f2c61e57`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow

REPLAY_STATUSES = ("pending", "running", "completed", "failed")
FINISHED_REPLAY_STATUSES = frozenset({"completed", "failed"})


class Replay(Base):
    __tablename__ = "replays"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="ck_replay_status"),
        CheckConstraint(
            "result_status IS NULL OR result_status IN ('ok', 'error', 'timeout')", name="ck_replay_result_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    original_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evaluation_results.id"), nullable=False, index=True
    )
    original_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_runs.id"), nullable=False)
    test_case_id: Mapped[str] = mapped_column(String(36), ForeignKey("test_cases.id"), nullable=False)
    dataset_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=False)
    # Computed by the worker from the (immutable) dataset version when it runs.
    dataset_content_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Copied from the original run: what the replay executes and how it scores.
    adapter_type: Mapped[str] = mapped_column(String(20), nullable=False)
    adapter_target: Mapped[str] = mapped_column(String(2000), nullable=False)
    evaluators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    provider_type: Mapped[str] = mapped_column(String(100), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    max_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # As requested; the worker validates them against the adapter's declaration.
    overrides: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # Why the replay itself failed (overrides rejected, adapter unloadable, ...).
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)

    # The replayed case's outcome, shaped like an EvaluationResult.
    result_status: Mapped[str | None] = mapped_column(String(10), nullable=True)
    passed: Mapped[bool | None] = mapped_column(nullable=True)
    output_answer: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieved_doc_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    citations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    case_error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # What the replay ran with (agentforge_worker.provenance), set when it starts.
    code_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    code_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pricing_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    metrics: Mapped[list[ReplayMetricScore]] = relationship(
        back_populates="replay", cascade="all, delete-orphan", order_by="ReplayMetricScore.position"
    )
    steps: Mapped[list[ReplayStep]] = relationship(
        back_populates="replay", cascade="all, delete-orphan", order_by="ReplayStep.step_index"
    )


class ReplayMetricScore(Base):
    """One evaluator's verdict on the replayed case (same columns as metric_scores)."""

    __tablename__ = "replay_metric_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    replay_id: Mapped[str] = mapped_column(String(36), ForeignKey("replays.id"), nullable=False, index=True)
    # Order the evaluators ran in (the original run's order for the same config).
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluator_name: Mapped[str] = mapped_column(String(100), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    passed: Mapped[bool | None] = mapped_column(nullable=True)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    labels: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    replay: Mapped[Replay] = relationship(back_populates="metrics")


class ReplayStep(Base):
    """One step the agent reported during the replay (same columns as agent_steps)."""

    __tablename__ = "replay_steps"
    __table_args__ = (
        UniqueConstraint("replay_id", "step_index", name="uq_replay_step_index"),
        CheckConstraint("kind IN ('retrieval', 'tool_call', 'final_answer')", name="ck_replay_step_kind"),
        CheckConstraint("step_index >= 1", name="ck_replay_step_index_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    replay_id: Mapped[str] = mapped_column(String(36), ForeignKey("replays.id"), nullable=False, index=True)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    args: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieved_doc_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    output: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    replay: Mapped[Replay] = relationship(back_populates="steps")


class ReplaySpan(Base):
    """One span recorded for the replay (same columns as trace_spans), stored
    after the same PII redaction as every other span."""

    __tablename__ = "replay_spans"
    __table_args__ = (
        UniqueConstraint("trace_id", "span_id", name="uq_replay_span"),
        CheckConstraint("status_code IN ('UNSET', 'OK', 'ERROR')", name="ck_replay_span_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    replay_id: Mapped[str] = mapped_column(String(36), ForeignKey("replays.id"), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    span_id: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    service: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status_code: Mapped[str] = mapped_column(String(10), nullable=False)
    status_message: Mapped[str | None] = mapped_column(String, nullable=True)
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
