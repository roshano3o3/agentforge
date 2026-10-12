"""Re-score state machine and read models, shared by the API and the worker.

    pending -> running -> completed | failed
    pending -> failed          (couldn't be queued)

completed and failed are terminal: `transition` refuses to leave them and `assert_accepts_outcome`
refuses writes; the `a9c3e7d15b42` migration's triggers enforce the same underneath.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentforge_api.models.evaluation import EvaluationResult, EvaluationRun
from agentforge_api.models.rescore import FINISHED_RESCORE_STATUSES, Rescore, RescoreResult
from agentforge_core.schemas import (
    MetricScoreOut,
    RescoreOut,
    RescoreResultOut,
    RescoreSummaryOut,
    ResultStatus,
)
from agentforge_evaluators import resolve

_ALLOWED = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}


class RescoreStateError(Exception):
    pass


def transition(rescore: Rescore, to: str, *, error_message: str | None = None) -> None:
    if to not in _ALLOWED[rescore.status]:
        raise RescoreStateError(f"re-score {rescore.id}: cannot go from {rescore.status} to {to}")
    now = datetime.now(UTC)
    rescore.status = to
    if to == "running":
        rescore.started_at = now
    if to in FINISHED_RESCORE_STATUSES:
        rescore.completed_at = now
    if error_message is not None:
        rescore.error_message = error_message


def assert_accepts_outcome(rescore: Rescore) -> None:
    if rescore.status != "running":
        raise RescoreStateError(f"re-score {rescore.id} is {rescore.status}; results can only be written while running")


def swap_versions(pinned: list[str], requested: list[str]) -> tuple[list[str], dict[str, str]]:
    """The run's pinned list with each requested evaluator swapped in by name (appended if the run
    didn't pin that name), and name -> "old -> new" for what changed. Raises UnknownEvaluatorError."""
    out = list(pinned)
    replaced: dict[str, str] = {}
    for spec in requested:
        new = resolve(spec).key
        name = new.partition("@")[0]
        at = next((i for i, k in enumerate(out) if k.partition("@")[0] == name), None)
        if at is None:
            out.append(new)
            replaced[name] = f"(not pinned) -> {new}"
        elif out[at] != new:
            replaced[name] = f"{out[at]} -> {new}"
            out[at] = new
    return out, replaced


async def load_rescore(session: AsyncSession, rescore_id: str) -> Rescore | None:
    return await session.scalar(
        select(Rescore)
        .where(Rescore.id == rescore_id)
        .options(selectinload(Rescore.results).selectinload(RescoreResult.metrics))
    )


async def _originals(session: AsyncSession, run_id: str) -> dict[str, EvaluationResult]:
    rows = await session.scalars(
        select(EvaluationResult)
        .where(EvaluationResult.evaluation_run_id == run_id)
        .options(selectinload(EvaluationResult.test_case))
    )
    return {r.id: r for r in rows}


async def to_summary(session: AsyncSession, rescore: Rescore) -> RescoreSummaryOut:
    run = await session.get(EvaluationRun, rescore.original_run_id)
    assert run is not None
    originals = await _originals(session, run.id)
    newly_passing = newly_failing = 0
    for r in rescore.results:
        before = originals[r.original_result_id].passed
        newly_passing += bool(r.passed and not before)
        newly_failing += bool(before and not r.passed)
    return RescoreSummaryOut(
        id=rescore.id,
        original_run_id=rescore.original_run_id,
        status=rescore.status,  # type: ignore[arg-type]
        requested_evaluators=list(rescore.requested_evaluators),
        evaluators=list(rescore.evaluators),
        replaced=dict(rescore.replaced),
        error_message=rescore.error_message,
        aggregates=rescore.aggregates,
        original_aggregates=run.aggregates,
        newly_passing=newly_passing,
        newly_failing=newly_failing,
        code_version=rescore.code_version,
        created_at=rescore.created_at,
        completed_at=rescore.completed_at,
    )


async def to_out(session: AsyncSession, rescore: Rescore) -> RescoreOut:
    summary = await to_summary(session, rescore)
    originals = await _originals(session, rescore.original_run_id)
    results = []
    for r in rescore.results:
        o = originals[r.original_result_id]
        results.append(
            RescoreResultOut(
                original_result_id=o.id,
                case_key=o.test_case.case_key,
                status=ResultStatus(o.status.value),
                original_passed=o.passed,
                passed=r.passed,
                changed=bool(o.passed) != r.passed,
                metrics=[MetricScoreOut.model_validate(m) for m in r.metrics],
                latency_ms=o.latency_ms,
                input_tokens=o.input_tokens,
                output_tokens=o.output_tokens,
                model=o.model,
                safety=o.test_case.safety,
            )
        )
    results.sort(key=lambda x: x.case_key)
    return RescoreOut(**summary.model_dump(), started_at=rescore.started_at, results=results)
