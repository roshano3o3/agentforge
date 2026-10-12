"""Re-scores: a finished run's stored outputs scored again with other evaluator versions.

A re-score never calls an adapter. The worker reads each stored result of the original run (the
answer, the reported steps, tokens, latency) and the frozen test case, and runs the evaluators --
the run's pinned versions with the requested ones swapped in by name -- exactly as the run did. The
verdicts land in their own tables (`rescores`, `rescore_results`, `rescore_metric_scores`), linked to
the original run and results, which are only read.

pending -> running -> completed | failed, like a run. Only the worker writes the outcome; once
completed or failed the re-score and everything under it is frozen (service layer + DB triggers,
migration `a9c3e7d15b42`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow

RESCORE_STATUSES = ("pending", "running", "completed", "failed")
FINISHED_RESCORE_STATUSES = frozenset({"completed", "failed"})


class Rescore(Base):
    __tablename__ = "rescores"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed')", name="ck_rescore_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    original_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evaluation_runs.id"), nullable=False, index=True
    )
    # As requested (name or name@version), and the full pinned list the re-score scores with.
    requested_evaluators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    evaluators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # name -> "old_key -> new_key" for each evaluator whose version changed (or "added").
    replaced: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    aggregates: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # What the worker scored with (agentforge_worker.provenance).
    code_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    code_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list[RescoreResult]] = relationship(back_populates="rescore", cascade="all, delete-orphan")


class RescoreResult(Base):
    """One original result's new case verdict. A result whose case errored or timed out keeps that
    status and has no scores, as in the run."""

    __tablename__ = "rescore_results"
    __table_args__ = (UniqueConstraint("rescore_id", "original_result_id", name="uq_rescore_result"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rescore_id: Mapped[str] = mapped_column(String(36), ForeignKey("rescores.id"), nullable=False, index=True)
    original_result_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_results.id"), nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    rescore: Mapped[Rescore] = relationship(back_populates="results")
    metrics: Mapped[list[RescoreMetricScore]] = relationship(
        back_populates="result", cascade="all, delete-orphan", order_by="RescoreMetricScore.position"
    )


class RescoreMetricScore(Base):
    """One evaluator's verdict in a re-score (same columns as metric_scores)."""

    __tablename__ = "rescore_metric_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # Denormalized so the immutability trigger can check the re-score's status directly.
    rescore_id: Mapped[str] = mapped_column(String(36), ForeignKey("rescores.id"), nullable=False, index=True)
    rescore_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rescore_results.id"), nullable=False, index=True
    )
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

    result: Mapped[RescoreResult] = relationship(back_populates="metrics")
