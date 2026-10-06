"""Replay state machine and read models, shared by the API and the worker.

    pending -> running -> completed | failed
    pending -> failed          (couldn't be queued, or rejected before it started)

Like runs: completed and failed are terminal, `transition` refuses to leave
them, `assert_accepts_outcome` refuses outcome writes outside `running`, and
the `failure_replay` migration's triggers enforce the same underneath.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from agentforge_api.models.dataset import TestCase
from agentforge_api.models.evaluation import EvaluationResult
from agentforge_api.models.replay import FINISHED_REPLAY_STATUSES, Replay, ReplaySpan
from agentforge_api.services.replay_diff import CaseSide, compute_diff
from agentforge_api.services.span_tree import span_tree
from agentforge_core.schemas import (
    FIXTURE_BASED_LABEL,
    LOCAL_DETERMINISTIC,
    AgentStepOut,
    MetricScoreOut,
    ReplayDiffOut,
    ReplayOut,
    ReplaySummaryOut,
    ResultStatus,
)

_ALLOWED: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"completed", "failed"}),
    "completed": frozenset(),
    "failed": frozenset(),
}


class ReplayStateError(Exception):
    """An attempted change to a replay that its current status doesn't allow."""


def transition(replay: Replay, to: str, *, error_message: str | None = None) -> None:
    if to not in _ALLOWED[replay.status]:
        raise ReplayStateError(f"replay {replay.id}: cannot go from {replay.status} to {to}")
    now = datetime.now(UTC)
    replay.status = to
    if to == "running":
        replay.started_at = now
    if to in FINISHED_REPLAY_STATUSES:
        replay.completed_at = now
    if error_message is not None:
        replay.error_message = error_message


def assert_accepts_outcome(replay: Replay) -> None:
    if replay.status != "running":
        raise ReplayStateError(f"replay {replay.id} is {replay.status}; its outcome can only be written while running")


async def load_replay(session: AsyncSession, replay_id: str) -> Replay | None:
    return await session.scalar(
        select(Replay)
        .where(Replay.id == replay_id)
        .options(selectinload(Replay.metrics), selectinload(Replay.steps))
        .execution_options(populate_existing=True)
    )


async def _original(session: AsyncSession, result_id: str) -> EvaluationResult:
    result = await session.scalar(
        select(EvaluationResult)
        .where(EvaluationResult.id == result_id)
        .options(selectinload(EvaluationResult.metrics), selectinload(EvaluationResult.steps))
    )
    assert result is not None
    return result


def _metrics_out(rows: list) -> list[MetricScoreOut]:
    return [MetricScoreOut.model_validate(m) for m in rows]


def _steps_out(rows: list) -> list[AgentStepOut]:
    return [AgentStepOut.model_validate(s) for s in sorted(rows, key=lambda s: s.step_index)]


def diff_for(replay: Replay, original: EvaluationResult) -> ReplayDiffOut | None:
    """Original vs replay, for a completed replay (None otherwise)."""
    if replay.status != "completed" or replay.result_status is None:
        return None
    before = CaseSide(
        status=ResultStatus(original.status.value),
        passed=original.passed,
        answer=original.output_answer,
        latency_ms=original.latency_ms,
        input_tokens=original.input_tokens,
        output_tokens=original.output_tokens,
        retrieved_doc_ids=list(original.retrieved_doc_ids),
        citations=list(original.citations),
        error_type=original.error_type,
        metrics=_metrics_out(sorted(original.metrics, key=lambda m: m.evaluator_name)),
        steps=_steps_out(original.steps),
    )
    after = CaseSide(
        status=ResultStatus(replay.result_status),
        passed=replay.passed,
        answer=replay.output_answer,
        latency_ms=replay.latency_ms,
        input_tokens=replay.input_tokens,
        output_tokens=replay.output_tokens,
        retrieved_doc_ids=list(replay.retrieved_doc_ids),
        citations=list(replay.citations),
        error_type=replay.error_type,
        metrics=_metrics_out(sorted(replay.metrics, key=lambda m: m.evaluator_name)),
        steps=_steps_out(replay.steps),
    )
    return compute_diff(before, after)


def _summary(replay: Replay, original: EvaluationResult, case_key: str, diff: ReplayDiffOut | None) -> ReplaySummaryOut:
    return ReplaySummaryOut(
        id=replay.id,
        original_result_id=replay.original_result_id,
        original_run_id=replay.original_run_id,
        case_key=case_key,
        overrides=replay.overrides,
        status=replay.status,  # type: ignore[arg-type]  # CHECK-constrained to the Literal values
        error_message=replay.error_message,
        result_status=ResultStatus(replay.result_status) if replay.result_status else None,
        passed=replay.passed,
        original_passed=original.passed,
        identical=diff.identical if diff else None,
        created_at=replay.created_at,
        completed_at=replay.completed_at,
    )


async def to_summary(session: AsyncSession, replay: Replay) -> ReplaySummaryOut:
    """`replay` must have been loaded with its metrics and steps (load_replay)."""
    original = await _original(session, replay.original_result_id)
    test_case = await session.get(TestCase, replay.test_case_id)
    assert test_case is not None
    return _summary(replay, original, test_case.case_key, diff_for(replay, original))


async def to_out(session: AsyncSession, replay: Replay) -> ReplayOut:
    """`replay` must have been loaded with its metrics and steps (load_replay)."""
    original = await _original(session, replay.original_result_id)
    test_case = await session.get(TestCase, replay.test_case_id)
    assert test_case is not None
    diff = diff_for(replay, original)
    summary = _summary(replay, original, test_case.case_key, diff)
    spans = list(await session.scalars(select(ReplaySpan).where(ReplaySpan.replay_id == replay.id)))
    steps_by_span = {s.span_id: s.step_index for s in replay.steps if s.span_id}
    return ReplayOut(
        **summary.model_dump(),
        test_case_id=replay.test_case_id,
        input=test_case.input,
        dataset_version_id=replay.dataset_version_id,
        dataset_content_hash=replay.dataset_content_hash,
        adapter_type=replay.adapter_type,
        adapter_target=replay.adapter_target,
        evaluators=replay.evaluators,
        labels=[FIXTURE_BASED_LABEL] if replay.provider_type == LOCAL_DETERMINISTIC else [],
        output_answer=replay.output_answer,
        retrieved_doc_ids=replay.retrieved_doc_ids,
        citations=replay.citations,
        input_tokens=replay.input_tokens,
        output_tokens=replay.output_tokens,
        model=replay.model,
        latency_ms=replay.latency_ms,
        error_type=replay.error_type,
        case_error_message=replay.case_error_message,
        metrics=_metrics_out(sorted(replay.metrics, key=lambda m: (m.evaluator_name, m.evaluator_version))),
        steps=_steps_out(replay.steps),
        trace_id=replay.trace_id,
        spans=span_tree(spans, steps_by_span),
        started_at=replay.started_at,
        diff=diff,
    )
