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
    running = "running"
    completed = "completed"
    failed = "failed"


class ResultStatus(str, enum.Enum):
    ok = "ok"
    error = "error"


class EvaluationRun(Base):
    """Immutable once `status` leaves `running`: no route updates a
    finished run's own fields. A replay (future phase) creates a brand new
    EvaluationRun linked back to the original — it never mutates this one.
    """

    __tablename__ = "evaluation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(String(36), ForeignKey("applications.id"), nullable=False)
    application_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("application_versions.id"), nullable=False
    )
    dataset_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=False)

    provider_type: Mapped[str] = mapped_column(String(100), nullable=False)
    evaluator_name: Mapped[str] = mapped_column(String(100), nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(50), nullable=False)
    environment: Mapped[str] = mapped_column(String(50), default="local", nullable=False)
    threshold: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    git_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)

    status: Mapped[RunStatus] = mapped_column(SAEnum(RunStatus), default=RunStatus.running, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    results: Mapped[list["EvaluationResult"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class EvaluationResult(Base):
    """Immutable once inserted — belongs to an immutable EvaluationRun."""

    __tablename__ = "evaluation_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    evaluation_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_runs.id"), nullable=False)
    test_case_id: Mapped[str] = mapped_column(String(36), ForeignKey("test_cases.id"), nullable=False)

    retrieved_doc_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    output_answer: Mapped[str | None] = mapped_column(String, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[ResultStatus] = mapped_column(SAEnum(ResultStatus), default=ResultStatus.ok, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    run: Mapped["EvaluationRun"] = relationship(back_populates="results")
    test_case: Mapped["TestCase"] = relationship()
