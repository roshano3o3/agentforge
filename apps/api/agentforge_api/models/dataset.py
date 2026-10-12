from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow
from agentforge_core.hashing import dataset_content_hash


class DatasetVersionStatus(str, enum.Enum):
    draft = "draft"
    published = "published"


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    versions: Mapped[list[DatasetVersion]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


class DatasetVersion(Base):
    """A dataset version is a two-state object: `draft` (mutable -- test
    cases can be added/edited/deleted via PATCH) or `published` (frozen
    forever -- PATCH returns 409). The transition is one-way: publishing
    (`POST .../publish`) sets `published_at` and flips status; nothing ever
    flips it back. This is enforced in the service layer (see
    routers/datasets.py) AND at the DB layer (see the
    `dataset_version_immutability` migration for the Postgres trigger /
    SQLite trigger set that blocks test_case mutation and un-publishing
    directly, independent of the API).

    Editing a published version is only ever done by creating a NEW draft
    version that copies its test cases (`POST .../new-draft`) -- there is no
    way to modify a published version's own row or its test cases, by any
    route.
    """

    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version", name="uq_dataset_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[DatasetVersionStatus] = mapped_column(
        SAEnum(DatasetVersionStatus), default=DatasetVersionStatus.draft, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Version-level evaluator config (see agentforge_evaluators.config). NULL
    # for versions written before per-case config: base = every run evaluator.
    default_evaluators: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    dataset: Mapped[Dataset] = relationship(back_populates="versions")
    test_cases: Mapped[list[TestCase]] = relationship(back_populates="dataset_version", cascade="all, delete-orphan")

    @property
    def content_hash(self) -> str | None:
        """sha256 of the frozen content (agentforge_core.hashing), for a
        published version only. Computed, not stored: the content it hashes
        can't change once published. Needs `test_cases` loaded."""
        if self.status != DatasetVersionStatus.published:
            return None
        return content_hash_of(self.default_evaluators, self.test_cases)


class TestCase(Base):
    """Mutable while the parent DatasetVersion is a draft; frozen once it's
    published (see DatasetVersion's docstring for how that's enforced).
    """

    __tablename__ = "test_cases"
    __table_args__ = (UniqueConstraint("dataset_version_id", "case_key", name="uq_case_key_per_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=False)
    case_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input: Mapped[str] = mapped_column(String, nullable=False)
    expected_answer: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_context: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Per-case evaluator config, laid over the version's default_evaluators.
    # NULL = inherit the default unchanged.
    evaluators: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Trajectory expectations for agent cases (expected_tools, forbidden_tools,
    # expected_sequence, ...; see agentforge_evaluators.trajectory). NULL for
    # cases with none. Frozen with the published version like every column.
    trajectory: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Legacy (Phase 2): answer assertions written before per-case config. Kept
    # read-only so published versions are never rewritten; the worker maps
    # them to answer_contains/answer_regex params, and new-draft converts
    # them into `evaluators`.
    expected_answer_contains: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expected_answer_regex: Mapped[str | None] = mapped_column(String, nullable=True)
    # Adversarial cases (Phase 5). `scenario`: the environment setup the
    # adapter receives (agentforge_core.scenario). `safety`: attack category,
    # source case and the safety evaluators' expectations -- never sent to the
    # adapter (agentforge_evaluators.safety). NULL for ordinary cases.
    scenario: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    safety: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    dataset_version: Mapped[DatasetVersion] = relationship(back_populates="test_cases")


def content_hash_of(default_evaluators: dict | None, cases: list[TestCase]) -> str:
    return dataset_content_hash(
        default_evaluators,
        [
            {
                "case_key": c.case_key,
                "input": c.input,
                "expected_answer": c.expected_answer,
                "expected_context": c.expected_context,
                "tags": c.tags,
                "evaluators": c.evaluators,
                "trajectory": c.trajectory,
                "scenario": c.scenario,
                "safety": c.safety,
                "expected_answer_contains": c.expected_answer_contains,
                "expected_answer_regex": c.expected_answer_regex,
            }
            for c in cases
        ],
    )
