"""Shared Pydantic v2 request/response contracts for the AgentForge API.

This module is imported by both the API (apps/api) and the CLI (cli/), so
the two never drift on wire-format field names. It has no dependency on
SQLAlchemy or any other server-side machinery -- it is pure data contracts.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class RunStatus(str, Enum):
    """pending (queued) -> running (worker picked it up) -> completed | failed.
    completed and failed are terminal and immutable."""

    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.completed, RunStatus.failed})


class ResultStatus(str, Enum):
    ok = "ok"
    error = "error"
    timeout = "timeout"


# The provider type whose results must always be shown as fixture-based.
LOCAL_DETERMINISTIC = "local-deterministic"
FIXTURE_BASED_LABEL = "fixture-based"


class DatasetVersionStatus(str, Enum):
    draft = "draft"
    published = "published"


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
# Dataset. A DatasetVersion is `draft` (mutable) until published, then
# frozen forever -- see DatasetVersion's docstring in models/dataset.py.
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
    expected_answer_contains: list[str] = Field(default_factory=list)
    expected_answer_regex: str | None = None
    expected_context: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("expected_context", "tags", "expected_answer_contains")
    @classmethod
    def no_blank_entries(cls, v: list[str]) -> list[str]:
        if any(not item.strip() for item in v):
            raise ValueError("list entries must not be blank")
        return v

    @field_validator("expected_answer_regex")
    @classmethod
    def valid_regex(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"expected_answer_regex is not a valid regular expression: {exc}") from exc
        return v


class DatasetVersionTestCasesRequest(BaseModel):
    """Body shape for both creating a draft (POST .../versions) and editing
    one (PATCH .../versions/{version}). PATCH replaces the draft's entire
    test-case set with exactly this list -- the server diffs it against
    what's currently stored (by case_key) and applies inserts/updates/
    deletes accordingly; it is not a partial merge. An empty list is valid
    (an empty draft, or "delete everything currently in this draft").
    """

    test_cases: list[TestCaseIn] = Field(default_factory=list)

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
    expected_answer_contains: list[str]
    expected_answer_regex: str | None
    expected_context: list[str]
    tags: list[str]

    model_config = {"from_attributes": True}


class DatasetVersionOut(BaseModel):
    id: str
    dataset_id: str
    version: int
    status: DatasetVersionStatus
    created_at: datetime
    published_at: datetime | None
    test_cases: list[TestCaseOut]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Evaluation runs / results. Runs are created by clients but executed only by
# the worker: there is deliberately no endpoint for a client to submit
# results or scores.
# ---------------------------------------------------------------------------

_PYTHON_TARGET = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


class AdapterSpec(BaseModel):
    type: Literal["python", "http"]
    target: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def target_matches_type(self) -> "AdapterSpec":
        if self.type == "python" and not _PYTHON_TARGET.match(self.target):
            raise ValueError("python adapter target must be 'module.path:function_name'")
        if self.type == "http" and not self.target.startswith(("http://", "https://")):
            raise ValueError("http adapter target must be an http:// or https:// URL")
        return self


class EvaluationRunCreate(BaseModel):
    application_id: str
    application_version_id: str
    dataset_version_id: str
    adapter: AdapterSpec
    # `name@version` or bare `name` (resolved to its latest version and stored
    # pinned). None = every registered evaluator.
    evaluators: list[str] | None = None
    provider_type: str = Field(default=LOCAL_DETERMINISTIC, min_length=1, max_length=100)
    environment: str = Field(default="local", max_length=50)
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_latency_ms: float | None = Field(default=None, gt=0)
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    git_commit_sha: str | None = Field(default=None, max_length=40)


class MetricScoreOut(BaseModel):
    evaluator_name: str
    evaluator_version: str
    score: float | None
    value: float | None
    unit: str | None
    passed: bool | None
    reason: str
    evidence: dict
    labels: list[str]

    model_config = {"from_attributes": True}


class EvaluationResultOut(BaseModel):
    id: str
    test_case_id: str
    case_key: str
    input: str
    expected_answer: str | None
    expected_context: list[str]
    retrieved_doc_ids: list[str]
    citations: list[str]
    output_answer: str | None
    input_tokens: int | None
    output_tokens: int | None
    model: str | None
    latency_ms: float
    status: ResultStatus
    passed: bool | None
    error_type: str | None
    error_message: str | None
    metrics: list[MetricScoreOut]


class RunProgress(BaseModel):
    completed_cases: int
    total_cases: int


class EvaluationRunSummaryOut(BaseModel):
    id: str
    application_name: str
    application_version: str
    dataset_name: str
    dataset_version: int
    provider_type: str
    # ["fixture-based"] for local-deterministic runs: results come from a
    # synthetic app and deterministic heuristics, not a real model.
    labels: list[str]
    evaluators: list[str]
    status: RunStatus
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    progress: RunProgress
    # Stored once when the run finishes (see agentforge_evaluators.aggregate
    # for definitions). None while pending/running.
    aggregates: dict | None


class EvaluationRunOut(EvaluationRunSummaryOut):
    application_id: str
    application_version_id: str
    dataset_version_id: str
    adapter_type: str | None
    adapter_target: str | None
    environment: str
    threshold: float
    max_latency_ms: float | None
    timeout_seconds: float | None
    git_commit_sha: str | None
    results: list[EvaluationResultOut] = Field(default_factory=list)


class EvaluatorOut(BaseModel):
    name: str
    version: str
    key: str
    kind: str
    description: str


