from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agentforge_api.db.base import Base
from agentforge_api.models._shared import new_id, utcnow


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    versions: Mapped[list["DatasetVersion"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


class DatasetVersion(Base):
    """Immutable once created: no API route ever updates a row here after
    insert. Republishing a dataset always creates a new, higher `version`
    rather than editing an existing one, so historical evaluation runs keep
    referencing exactly the test cases they were evaluated against.
    """

    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version", name="uq_dataset_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_id: Mapped[str] = mapped_column(String(36), ForeignKey("datasets.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    dataset: Mapped["Dataset"] = relationship(back_populates="versions")
    test_cases: Mapped[list["TestCase"]] = relationship(
        back_populates="dataset_version", cascade="all, delete-orphan"
    )


class TestCase(Base):
    """Immutable once created — belongs to an immutable DatasetVersion."""

    __tablename__ = "test_cases"
    __table_args__ = (UniqueConstraint("dataset_version_id", "case_key", name="uq_case_key_per_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    dataset_version_id: Mapped[str] = mapped_column(String(36), ForeignKey("dataset_versions.id"), nullable=False)
    case_key: Mapped[str] = mapped_column(String(200), nullable=False)
    input: Mapped[str] = mapped_column(String, nullable=False)
    expected_answer: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_context: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    dataset_version: Mapped["DatasetVersion"] = relationship(back_populates="test_cases")
