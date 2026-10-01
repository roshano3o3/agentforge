from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow


class Baseline(Base):
    """A pointer: (application, environment) -> one completed run. Setting a
    baseline updates this row's `run_id` and nothing else -- no result, score
    or step is copied. The DB refuses a pointer to a run that isn't completed
    or belongs to another application (trigger in the `release_gate`
    migration)."""

    __tablename__ = "baselines"
    __table_args__ = (UniqueConstraint("application_id", "environment", name="uq_baseline_app_env"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(String(36), ForeignKey("applications.id"), nullable=False)
    environment: Mapped[str] = mapped_column(String(50), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_runs.id"), nullable=False)
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ReleaseDecision(Base):
    """One gate evaluation: the candidate and baseline runs, the exact policy
    applied, every check with its numbers, and the verdict. Computed by the
    API from persisted runs (a client supplies only the policy). Immutable
    from the moment it's written: a DB trigger blocks UPDATE and DELETE."""

    __tablename__ = "release_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(String(36), ForeignKey("applications.id"), nullable=False)
    candidate_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("evaluation_runs.id"), nullable=False, index=True
    )
    baseline_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("evaluation_runs.id"), nullable=False)
    # What the caller asked for: an environment name ("production") or a run id.
    baseline_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    passed: Mapped[bool] = mapped_column(nullable=False)
    checks: Mapped[list[dict]] = mapped_column(JSON, nullable=False)
    # The regression report the checks were computed from.
    regression: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
