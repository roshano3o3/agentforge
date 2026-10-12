from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow


class Baseline(Base):
    """A pointer: (application, environment, dataset) -> one completed run of
    that dataset, so `production` can name a different run per dataset (e.g.
    the trajectory dataset and the safety dataset). The dataset is always the
    run's own. Setting a baseline updates this row's `run_id` and nothing
    else -- no result, score or step is copied. The DB refuses a pointer to a
    run that isn't completed, belongs to another application, or is of
    another dataset (triggers in the `release_gate` and `baselines_per_dataset`
    migrations)."""

    __tablename__ = "baselines"
    __table_args__ = (
        UniqueConstraint("application_id", "environment", "dataset_id", name="uq_baseline_app_env_dataset"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(String(36), ForeignKey("applications.id"), nullable=False)
    environment: Mapped[str] = mapped_column(String(50), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id"), nullable=False)
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
