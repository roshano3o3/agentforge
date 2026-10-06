"""Shared Pydantic v2 request/response contracts for the AgentForge API.

This module is imported by both the API (apps/api) and the CLI (cli/), so
the two never drift on wire-format field names. It has no dependency on
SQLAlchemy or any other server-side machinery -- it is pure data contracts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import Enum
from typing import Any, Literal

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


# An evaluator config: {"<name or name@version>": {params} | true | false}.
# Validated against the evaluator registry by the API (and the CLI, offline);
# see packages/evaluators/agentforge_evaluators/config.py for semantics.
EvaluatorConfigIn = dict[str, Any]
# A test case's trajectory expectations; see
# packages/evaluators/agentforge_evaluators/trajectory.py for the keys.
TrajectoryIn = dict[str, Any]
# How the application's environment is set up for one case (mock tool
# failures, injected tool output, the session user); the only part of a case
# besides its input that the adapter receives. See agentforge_core.scenario.
ScenarioIn = dict[str, Any]
# Safety-test metadata and expectations (attack category, source case,
# forbidden actions, secrets, ...). Evaluators only; never sent to the
# adapter. See packages/evaluators/agentforge_evaluators/safety.py.
SafetyIn = dict[str, Any]


class TestCaseIn(BaseModel):
    # Unknown fields are rejected, not silently dropped: a typo'd or retired
    # field (e.g. Phase 2's expected_answer_contains) must not vanish quietly.
    model_config = {"extra": "forbid"}

    case_key: str = Field(min_length=1, max_length=200)
    input: str = Field(min_length=1)
    expected_answer: str | None = None
    expected_context: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # None = use the dataset version's default_evaluators as-is.
    evaluators: EvaluatorConfigIn | None = None
    # Trajectory expectations for agent cases (expected_tools, forbidden_tools,
    # expected_sequence, ...). Validated by the API and the CLI against
    # agentforge_evaluators.validate_trajectory.
    trajectory: TrajectoryIn | None = None
    # Adversarial cases (Phase 5): the environment setup sent to the adapter,
    # and the safety block only evaluators read. Both validated by the API and the CLI.
    scenario: ScenarioIn | None = None
    safety: SafetyIn | None = None

    @field_validator("expected_context", "tags")
    @classmethod
    def no_blank_entries(cls, v: list[str]) -> list[str]:
        if any(not item.strip() for item in v):
            raise ValueError("list entries must not be blank")
        return v


class DatasetVersionTestCasesRequest(BaseModel):
    """Body shape for both creating a draft (POST .../versions) and editing
    one (PATCH .../versions/{version}). PATCH replaces the draft's entire
    test-case set with exactly this list -- the server diffs it against
    what's currently stored (by case_key) and applies inserts/updates/
    deletes accordingly; it is not a partial merge. An empty list is valid
    (an empty draft, or "delete everything currently in this draft").

    `default_evaluators` is the version-level evaluator config. On PATCH it
    is only changed when the field is present in the body (null clears it).
    """

    model_config = {"extra": "forbid"}

    test_cases: list[TestCaseIn] = Field(default_factory=list)
    default_evaluators: EvaluatorConfigIn | None = None

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
    evaluators: EvaluatorConfigIn | None
    trajectory: TrajectoryIn | None
    scenario: ScenarioIn | None = None
    safety: SafetyIn | None = None
    # Read-only legacy fields from Phase 2 datasets (published before per-case
    # config existed). The worker still honors them for those versions; new
    # data can't set them -- use `evaluators` instead.
    expected_answer_contains: list[str]
    expected_answer_regex: str | None

    model_config = {"from_attributes": True}


class DatasetVersionOut(BaseModel):
    id: str
    dataset_id: str
    version: int
    status: DatasetVersionStatus
    created_at: datetime
    published_at: datetime | None
    default_evaluators: EvaluatorConfigIn | None
    # sha256 of the version's content (agentforge_core.hashing); set only once
    # published, since a draft's content can still change.
    content_hash: str | None = None
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
    def target_matches_type(self) -> AdapterSpec:
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


class AgentStepOut(BaseModel):
    """One step of the trajectory the agent reported, exactly as stored.
    `step_index` is 1-based over all steps -- the "step N" in evaluator
    reasons and in their evidence's `failing_steps`."""

    step_index: int
    kind: Literal["retrieval", "tool_call", "final_answer"]
    name: str
    args: dict
    result: Any
    error: str | None
    retrieved_doc_ids: list[str]
    output: str | None
    duration_ms: float | None
    # The span the agent reported for this step (see GET /traces/{result_id}).
    span_id: str | None = None

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
    # The case's trajectory expectations (from the frozen dataset version) and
    # the steps the agent reported, in order. Empty for non-agent cases.
    trajectory: TrajectoryIn | None = None
    steps: list[AgentStepOut] = Field(default_factory=list)
    # The case's safety block (attack category, source case, ...), if any.
    safety: SafetyIn | None = None


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
    # Content hash of the (published, frozen) dataset version this run used.
    dataset_content_hash: str | None = None
    results: list[EvaluationResultOut] = Field(default_factory=list)


class EvaluatorOut(BaseModel):
    name: str
    version: str
    key: str
    kind: str
    description: str


# ---------------------------------------------------------------------------
# Baselines, regression, release gate (Phase 4). The regression arithmetic
# and check definitions live in agentforge_evaluators.release.
# ---------------------------------------------------------------------------


class BaselineSet(BaseModel):
    """Point (the run's application, `environment`) at a completed run."""

    model_config = {"extra": "forbid"}

    run_id: str
    environment: str = Field(min_length=1, max_length=50)


class BaselineOut(BaseModel):
    application_id: str
    application_name: str
    environment: str
    run_id: str
    set_at: datetime
    # Context about the run it points to.
    application_version: str
    dataset_name: str
    dataset_version: int
    pass_rate: float | None


class ReleaseDecisionCreate(BaseModel):
    """Evaluate a candidate run against a baseline under `policy` (the
    `release_policy` block of agentforge.yaml). `baseline` is an environment
    name (resolved through the candidate's application's baseline pointer) or
    a run id. The API computes every check; the client supplies no numbers."""

    model_config = {"extra": "forbid"}

    candidate_run_id: str
    baseline: str = Field(min_length=1, max_length=100)
    policy: dict[str, Any]


class GateCheckOut(BaseModel):
    kind: Literal["minimum", "maximum", "regression", "cases"]
    metric: str
    rule: str
    threshold: float | None
    baseline: float | None
    candidate: float | None
    delta: float | None
    delta_pct: float | None
    passed: bool
    reason: str


class ReleaseDecisionOut(BaseModel):
    id: str
    application_id: str
    application_name: str
    candidate_run_id: str
    baseline_run_id: str
    baseline_ref: str
    policy: dict[str, Any]
    passed: bool
    checks: list[GateCheckOut]
    # The regression report the checks were computed from (see RegressionOut).
    regression: dict[str, Any]
    created_at: datetime


class MetricDeltaOut(BaseModel):
    baseline: float | None
    candidate: float | None
    delta: float | None
    delta_pct: float | None


class EvaluatorDeltaOut(BaseModel):
    name: str
    unit: str | None
    baseline_version: str | None
    candidate_version: str | None
    # False when the evaluator is missing from one run or ran at different versions.
    comparable: bool
    mean_score: MetricDeltaOut
    pass_rate: MetricDeltaOut
    mean_value: MetricDeltaOut


class CaseChangeOut(BaseModel):
    case_key: str
    tags: list[str]
    baseline_status: ResultStatus
    candidate_status: ResultStatus
    candidate_failed_evaluators: list[str]


class RegressionOut(BaseModel):
    baseline_run_id: str
    candidate_run_id: str
    dataset_version_id: str
    # pass_rate, error_rate, p50/p95_latency_ms, estimated_cost_usd (ESTIMATED).
    summary: dict[str, MetricDeltaOut]
    metrics: list[EvaluatorDeltaOut]
    # Pass rate per attack category (adversarial datasets only).
    safety: dict[str, MetricDeltaOut] = Field(default_factory=dict)
    # newly_failing / fixed / still_failing / still_passing
    cases: dict[str, list[CaseChangeOut]]
    case_counts: dict[str, int]


# ---------------------------------------------------------------------------
# Traces (Phase 6): stored OpenTelemetry spans, as a tree per evaluated case.
# ---------------------------------------------------------------------------


class TraceSpanOut(BaseModel):
    span_id: str
    parent_span_id: str | None
    name: str
    kind: str
    # The recording process (agentforge-api / agentforge-worker); the
    # `agentforge.component` attribute says api / worker / agent.
    service: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    attributes: dict[str, Any]
    status_code: Literal["UNSET", "OK", "ERROR"]
    status_message: str | None
    events: list[dict[str, Any]]
    # The agent step (agent_steps.step_index) that reported this span, if any.
    step_index: int | None = None
    children: list[TraceSpanOut] = Field(default_factory=list)


class TraceOut(BaseModel):
    result_id: str  # the case's result id in its run (the path parameter)
    case_key: str
    run_id: str
    trace_id: str
    span_count: int
    spans: list[TraceSpanOut]  # roots (normally one: agentforge.run.create)


# -- failure replay -------------------------------------------------------------------

_OVERRIDE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_OVERRIDES_CHARS = 200_000


class ReplayCreate(BaseModel):
    """Re-run one evaluated case. `overrides` are checked here only for shape
    (names, JSON, size); what they may contain is up to the adapter, which
    declares its settings -- the worker validates against that declaration and
    rejects the replay with a message if they don't match."""

    result_id: str = Field(min_length=1, max_length=36)
    overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("overrides")
    @classmethod
    def overrides_shape(cls, value: dict[str, Any]) -> dict[str, Any]:
        bad = [k for k in value if not _OVERRIDE_KEY.match(k)]
        if bad:
            raise ValueError(f"override names must be identifiers (letters, digits, _): {bad}")
        try:
            size = len(json.dumps(value, allow_nan=False))
        except ValueError as exc:
            raise ValueError(f"overrides must be JSON: {exc}") from exc
        if size > MAX_OVERRIDES_CHARS:
            raise ValueError(f"overrides are {size} characters of JSON; at most {MAX_OVERRIDES_CHARS} allowed")
        return value


ReplayStatus = Literal["pending", "running", "completed", "failed"]


class ReplaySummaryOut(BaseModel):
    id: str
    original_result_id: str
    original_run_id: str
    case_key: str
    overrides: dict[str, Any]
    status: ReplayStatus
    error_message: str | None
    result_status: ResultStatus | None
    passed: bool | None
    original_passed: bool | None
    # Completed replays only: every step, tool call, answer and evaluator
    # verdict matched the original (see ReplayDiffOut.identical).
    identical: bool | None = None
    created_at: datetime
    completed_at: datetime | None


class MetricBrief(BaseModel):
    evaluator_version: str
    passed: bool | None
    score: float | None
    value: float | None
    unit: str | None
    reason: str


class EvaluatorChange(BaseModel):
    evaluator_name: str
    # unchanged | fixed (fail -> pass) | regressed (pass -> fail) | changed
    # (score/value/reason/applicability) | added | removed
    change: Literal["unchanged", "fixed", "regressed", "changed", "added", "removed"]
    before: MetricBrief | None
    after: MetricBrief | None


class StepBrief(BaseModel):
    step_index: int
    kind: str
    name: str
    args: dict
    result: Any
    error: str | None
    output: str | None
    retrieved_doc_ids: list[str]


class StepChange(BaseModel):
    # Steps are aligned by (kind, tool name) in order; an aligned pair is
    # unchanged or changed (changed_fields lists what differs).
    op: Literal["unchanged", "changed", "added", "removed"]
    kind: str
    name: str
    before: StepBrief | None
    after: StepBrief | None
    changed_fields: list[str] = Field(default_factory=list)


class TextSegment(BaseModel):
    op: Literal["equal", "insert", "delete"]
    text: str


class AnswerDiff(BaseModel):
    before: str | None
    after: str | None
    changed: bool
    segments: list[TextSegment]  # word-level, in order


class NumberChange(BaseModel):
    before: float | None
    after: float | None
    delta: float | None  # after - before, when both are known


class ReplayDiffOut(BaseModel):
    """Original result vs replay. `identical` ignores what's measured rather
    than produced -- latency (and the latency evaluator's measured value and
    reason), step timings and span ids -- and compares everything else:
    status, verdict, answer, retrieved docs, citations, tokens, every step
    (kind, tool, args, result, error, output) and every evaluator's version,
    verdict, score, value and reason."""

    identical: bool
    differences: list[str]
    before_status: ResultStatus
    after_status: ResultStatus
    before_passed: bool | None
    after_passed: bool | None
    evaluators: list[EvaluatorChange]
    trajectory: list[StepChange]
    answer: AnswerDiff
    latency_ms: NumberChange
    input_tokens: NumberChange
    output_tokens: NumberChange


class ReplayOut(ReplaySummaryOut):
    test_case_id: str
    input: str
    dataset_version_id: str
    dataset_content_hash: str | None
    adapter_type: str
    adapter_target: str
    evaluators: list[str]
    labels: list[str]
    output_answer: str | None
    retrieved_doc_ids: list[str]
    citations: list[str]
    input_tokens: int | None
    output_tokens: int | None
    model: str | None
    latency_ms: float | None
    error_type: str | None
    case_error_message: str | None
    metrics: list[MetricScoreOut]
    steps: list[AgentStepOut]
    trace_id: str | None
    spans: list[TraceSpanOut]  # the replay's span tree (redacted like every stored span)
    started_at: datetime | None
    diff: ReplayDiffOut | None  # completed replays only
