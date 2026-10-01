"""Evaluation-run state machine and read models, shared by the API and the worker.

    pending -> running -> completed | failed
    pending -> failed          (e.g. the run couldn't be enqueued)

completed and failed are terminal: `transition` refuses to leave them and
`assert_accepts_results` refuses result writes, so a finished run can't be
changed through application code. The `evaluation_engine` migration's DB
triggers enforce the same rule underneath, independent of this module.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentforge_api.models.application import Application, ApplicationVersion
from agentforge_api.models.dataset import Dataset, DatasetVersion, TestCase, content_hash_of
from agentforge_api.models.evaluation import (
    TERMINAL_STATUSES,
    EvaluationResult,
    EvaluationRun,
    RunStatus,
)
from agentforge_core import schemas
from agentforge_core.schemas import (
    FIXTURE_BASED_LABEL,
    LOCAL_DETERMINISTIC,
    AgentStepOut,
    EvaluationResultOut,
    EvaluationRunOut,
    EvaluationRunSummaryOut,
    MetricScoreOut,
    RunProgress,
)
from agentforge_evaluators import CaseRecord, MetricRecord, compute_aggregates

_ALLOWED: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.pending: frozenset({RunStatus.running, RunStatus.failed}),
    RunStatus.running: frozenset({RunStatus.completed, RunStatus.failed}),
    RunStatus.completed: frozenset(),
    RunStatus.failed: frozenset(),
}


class RunStateError(Exception):
    """An attempted change to a run that its current status doesn't allow."""


def transition(run: EvaluationRun, to: RunStatus, *, error_message: str | None = None) -> None:
    if to not in _ALLOWED[run.status]:
        raise RunStateError(f"run {run.id}: cannot go from {run.status.value} to {to.value}")
    now = datetime.now(UTC)
    run.status = to
    if to == RunStatus.running:
        run.started_at = now
    if to in TERMINAL_STATUSES:
        run.completed_at = now
    if error_message is not None:
        run.error_message = error_message


def assert_accepts_results(run: EvaluationRun) -> None:
    if run.status != RunStatus.running:
        raise RunStateError(f"run {run.id} is {run.status.value}; results can only be written while running")


def run_labels(run: EvaluationRun) -> list[str]:
    return [FIXTURE_BASED_LABEL] if run.provider_type == LOCAL_DETERMINISTIC else []


def case_records(results: list[EvaluationResult]) -> list[CaseRecord]:
    return [
        CaseRecord(
            status=r.status.value,
            passed=bool(r.passed),
            latency_ms=float(r.latency_ms),
            metrics=[
                MetricRecord(
                    evaluator_name=m.evaluator_name,
                    evaluator_version=m.evaluator_version,
                    score=m.score,
                    value=m.value,
                    unit=m.unit,
                    passed=m.passed,
                    reason=m.reason,
                )
                for m in r.metrics
            ],
            category=(r.test_case.safety or {}).get("category"),
        )
        for r in results
    ]


def _results_query():
    return selectinload(EvaluationRun.results).options(
        selectinload(EvaluationResult.test_case),
        selectinload(EvaluationResult.metrics),
        selectinload(EvaluationResult.steps),
    )


async def load_run(session: AsyncSession, run_id: str, *, with_results: bool = True) -> EvaluationRun | None:
    query = select(EvaluationRun).where(EvaluationRun.id == run_id)
    if with_results:
        query = query.options(_results_query())
    return await session.scalar(query)


async def _context(session: AsyncSession, run: EvaluationRun) -> dict:
    app = await session.get(Application, run.application_id)
    app_version = await session.get(ApplicationVersion, run.application_version_id)
    dataset_version = await session.get(DatasetVersion, run.dataset_version_id)
    assert app and app_version and dataset_version
    dataset = await session.get(Dataset, dataset_version.dataset_id)
    assert dataset
    total = await session.scalar(
        select(func.count()).select_from(TestCase).where(TestCase.dataset_version_id == run.dataset_version_id)
    )
    done = await session.scalar(
        select(func.count()).select_from(EvaluationResult).where(EvaluationResult.evaluation_run_id == run.id)
    )
    return {
        "application_name": app.name,
        "application_version": app_version.version,
        "dataset_name": dataset.name,
        "dataset_version": dataset_version.version,
        "progress": RunProgress(completed_cases=done or 0, total_cases=total or 0),
    }


async def _aggregates(session: AsyncSession, run: EvaluationRun) -> dict | None:
    if run.aggregates is not None:
        return run.aggregates
    if run.status not in TERMINAL_STATUSES:
        return None
    # Phase 1 runs predate stored aggregates: compute them from their
    # persisted rows with the same function the worker uses.
    loaded = await load_run(session, run.id)
    assert loaded is not None
    return compute_aggregates(case_records(loaded.results))


async def to_summary(session: AsyncSession, run: EvaluationRun) -> EvaluationRunSummaryOut:
    ctx = await _context(session, run)
    return EvaluationRunSummaryOut(
        id=run.id,
        provider_type=run.provider_type,
        labels=run_labels(run),
        evaluators=run.evaluators,
        status=schemas.RunStatus(run.status.value),
        error_message=run.error_message,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        aggregates=await _aggregates(session, run),
        **ctx,
    )


async def to_detail(session: AsyncSession, run: EvaluationRun) -> EvaluationRunOut:
    """`run` must have been loaded with results (see load_run)."""
    summary = await to_summary(session, run)
    results = sorted(run.results, key=lambda r: r.test_case.case_key)
    dataset_version = await session.get(DatasetVersion, run.dataset_version_id)
    assert dataset_version is not None
    all_cases = list(await session.scalars(select(TestCase).where(TestCase.dataset_version_id == dataset_version.id)))
    return EvaluationRunOut(
        **summary.model_dump(),
        application_id=run.application_id,
        application_version_id=run.application_version_id,
        dataset_version_id=run.dataset_version_id,
        adapter_type=run.adapter_type,
        adapter_target=run.adapter_target,
        environment=run.environment,
        threshold=run.threshold,
        max_latency_ms=run.max_latency_ms,
        timeout_seconds=run.timeout_seconds,
        git_commit_sha=run.git_commit_sha,
        # Runs only target published (frozen) versions, so this is that version's hash.
        dataset_content_hash=content_hash_of(dataset_version.default_evaluators, all_cases),
        results=[
            EvaluationResultOut(
                id=r.id,
                test_case_id=r.test_case_id,
                case_key=r.test_case.case_key,
                input=r.test_case.input,
                expected_answer=r.test_case.expected_answer,
                expected_context=r.test_case.expected_context,
                retrieved_doc_ids=r.retrieved_doc_ids,
                citations=r.citations,
                output_answer=r.output_answer,
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                model=r.model,
                latency_ms=r.latency_ms,
                status=schemas.ResultStatus(r.status.value),
                passed=r.passed,
                error_type=r.error_type,
                error_message=r.error_message,
                metrics=[
                    MetricScoreOut.model_validate(m)
                    for m in sorted(r.metrics, key=lambda m: (m.evaluator_name, m.evaluator_version))
                ],
                trajectory=r.test_case.trajectory,
                steps=[AgentStepOut.model_validate(s) for s in sorted(r.steps, key=lambda s: s.step_index)],
                safety=r.test_case.safety,
            )
            for r in results
        ],
    )
