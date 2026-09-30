from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow
from agentforge_api.models.dataset import TestCase


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


TERMINAL_STATUSES = frozenset({RunStatus.completed, RunStatus.failed})


class ResultStatus(str, enum.Enum):
    ok = "ok"
    error = "error"
    timeout = "timeout"


class EvaluationRun(Base):
    """pending -> running -> completed | failed. Only the worker executes a
    run and writes its results; no route lets a client submit them.

    Immutable once completed or failed: the service layer (services/runs.py)
    refuses any further transition or result write, and a DB trigger (the
    `evaluation_engine` migration) blocks UPDATE/DELETE of a finished run and
    INSERT/UPDATE/DELETE of its results and metric scores, independent of
    the application code.
    """

    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(String(36), ForeignKey("applications.id"), nullable=False)
    application_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("application_versions.id"), nullable=False
    )
    dataset_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=False)

    provider_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Pinned `name@version` of every evaluator this run uses.
    evaluators: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # NULL only for Phase 1 runs, which were executed client-side by the CLI.
    adapter_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    adapter_target: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    environment: Mapped[str] = mapped_column(String(50), default="local", nullable=False)
    threshold: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    git_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    status: Mapped[RunStatus] = mapped_column(SAEnum(RunStatus), default=RunStatus.pending, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    aggregates: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list["EvaluationResult"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class EvaluationResult(Base):
    """One test case's outcome within a run: the adapter's raw output (or the
    error/timeout that replaced it), plus one MetricScore per evaluator."""

    __tablename__ = "evaluation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evaluation_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_runs.id"), nullable=False)
    test_case_id: Mapped[str] = mapped_column(String(36), ForeignKey("test_cases.id"), nullable=False)

    output_answer: Mapped[str | None] = mapped_column(String, nullable=True)
    retrieved_doc_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    citations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[ResultStatus] = mapped_column(SAEnum(ResultStatus), default=ResultStatus.ok, nullable=False)
    # Case-level verdict: ok status and no evaluator returned passed=False.
    passed: Mapped[bool | None] = mapped_column(nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["EvaluationRun"] = relationship(back_populates="results")
    test_case: Mapped["TestCase"] = relationship()
    metrics: Mapped[list["MetricScore"]] = relationship(back_populates="result", cascade="all, delete-orphan")


class MetricScore(Base):
    """One evaluator's verdict on one case, with the evaluator version that
    produced it."""

    __tablename__ = "metric_scores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evaluation_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evaluation_results.id"), nullable=False
    )
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

    result: Mapped["EvaluationResult"] = relationship(back_populates="metrics")
