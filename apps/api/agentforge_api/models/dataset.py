from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow


class DatasetVersionStatus(str, enum.Enum):
    draft = "draft"
    published = "published"


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    versions: Mapped[list["DatasetVersion"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


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

    dataset: Mapped["Dataset"] = relationship(back_populates="versions")
    test_cases: Mapped[list["TestCase"]] = relationship(
        back_populates="dataset_version", cascade="all, delete-orphan"
    )


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
    expected_answer_contains: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    expected_answer_regex: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_context: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    dataset_version: Mapped["DatasetVersion"] = relationship(back_populates="test_cases")
