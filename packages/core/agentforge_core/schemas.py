"""Shared Pydantic v2 request/response contracts for the AgentForge API.

This module is imported by both the API (apps/api) and the CLI (cli/), so
the two never drift on wire-format field names. It has no dependency on
SQLAlchemy or any other server-side machinery -- it is pure data contracts.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class RunStatus(str, Enum):
    running = "running"
    completed = "completed"
    failed = "failed"


class ResultStatus(str, Enum):
    ok = "ok"
    error = "error"


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ApplicationOut(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApplicationVersionCreate(BaseModel):
    version: str = Field(min_length=1, max_length=100)
    description: str | None = None


class ApplicationVersionOut(BaseModel):
    id: str
    application_id: str
    version: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Dataset (immutable once published)
# ---------------------------------------------------------------------------


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class DatasetOut(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class TestCaseIn(BaseModel):
    case_key: str = Field(min_length=1, max_length=200)
    input: str = Field(min_length=1)
    expected_answer: str | None = None
    expected_context: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("expected_context", "tags")
    @classmethod
    def no_blank_entries(cls, v: list[str]) -> list[str]:
        if any(not item.strip() for item in v):
            raise ValueError("list entries must not be blank")
        return v


class DatasetVersionPublishRequest(BaseModel):
    test_cases: list[TestCaseIn] = Field(min_length=1)

    @field_validator("test_cases")
    @classmethod
    def unique_case_keys(cls, v: list[TestCaseIn]) -> list[TestCaseIn]:
        keys = [tc.case_key for tc in v]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate case_key(s) in dataset version: {dupes}")
        return v


class TestCaseOut(BaseModel):
    id: str
    case_key: str
    input: str
    expected_answer: str | None
    expected_context: list[str]
    tags: list[str]

    model_config = {"from_attributes": True}


class DatasetVersionOut(BaseModel):
    id: str
    dataset_id: str
    version: int
    published_at: datetime
    test_cases: list[TestCaseOut]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Evaluation runs / results
# ---------------------------------------------------------------------------


class EvaluationRunCreate(BaseModel):
    application_id: str
    application_version_id: str
    dataset_version_id: str
    provider_type: str = Field(min_length=1, max_length=100)
    evaluator_name: str = Field(min_length=1, max_length=100)
    evaluator_version: str = Field(min_length=1, max_length=50)
    environment: str = Field(default="local", max_length=50)
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    git_commit_sha: str | None = Field(default=None, max_length=40)


class EvaluationResultIn(BaseModel):
    test_case_id: str
    retrieved_doc_ids: list[str] = Field(default_factory=list)
    output_answer: str | None = None
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    passed: bool | None = None
    evidence: dict = Field(default_factory=dict)
    latency_ms: int = Field(ge=0)
    status: ResultStatus = ResultStatus.ok
    error_message: str | None = None


class EvaluationResultSubmitRequest(BaseModel):
    results: list[EvaluationResultIn] = Field(min_length=1)


class EvaluationResultOut(BaseModel):
    id: str
    test_case_id: str
    case_key: str
    input: str
    expected_context: list[str]
    retrieved_doc_ids: list[str]
    output_answer: str | None
    score: float | None
    passed: bool | None
    evidence: dict
    latency_ms: int
    status: ResultStatus
    error_message: str | None

    model_config = {"from_attributes": True}


class RunCompleteRequest(BaseModel):
    status: str = "completed"


class EvaluationRunOut(BaseModel):
    id: str
    application_id: str
    application_name: str
    application_version_id: str
    application_version: str
    dataset_version_id: str
    dataset_name: str
    dataset_version: int
    provider_type: str
    evaluator_name: str
    evaluator_version: str
    environment: str
    threshold: float
    git_commit_sha: str | None
    status: RunStatus
    created_at: datetime
    completed_at: datetime | None
    case_count: int
    mean_score: float | None
    pass_rate: float | None
    avg_latency_ms: float | None
    results: list[EvaluationResultOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class EvaluationRunSummaryOut(BaseModel):
    id: str
    application_name: str
    application_version: str
    dataset_name: str
    dataset_version: int
    evaluator_name: str
    evaluator_version: str
    provider_type: str
    status: RunStatus
    created_at: datetime
    completed_at: datetime | None
    case_count: int
    mean_score: float | None
    pass_rate: float | None
    avg_latency_ms: float | None

    model_config = {"from_attributes": True}
