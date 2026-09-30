"""Execute one evaluation run: pending -> running -> completed | failed.

Idempotent per run id. A finished run is left alone. A run found already
`running` (the previous attempt died mid-way) has its partial results
cleared and starts over, so a result set is always from one complete pass.

Each case's result is committed as soon as it's scored, so GET /runs/{id}
shows live progress. Results are always written *before* the final status
change: once a run is completed, the DB trigger refuses any further result
writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from agentforge_api.models.dataset import TestCase
from agentforge_api.models.evaluation import (
    TERMINAL_STATUSES,
    EvaluationResult,
    EvaluationRun,
    MetricScore,
    ResultStatus,
    RunStatus,
)
from agentforge_api.services import runs as run_service
from agentforge_evaluators import (
    EvalConfig,
    EvalInput,
    EvaluatorSpec,
    MetricOutcome,
    ModelPrice,
    compute_aggregates,
    resolve,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_worker.adapters import AdapterLoadError, CaseOutcome, build_adapter, invoke

log = logging.getLogger("agentforge.worker")


def score_case(
    specs: list[EvaluatorSpec], case: EvalInput, config: EvalConfig
) -> list[tuple[EvaluatorSpec, MetricOutcome]]:
    """Run every evaluator; an evaluator that raises is recorded as a failed
    metric for this case, never propagated."""
    scored = []
    for spec in specs:
        try:
            outcome = spec.fn(case, config)
        except Exception as exc:  # noqa: BLE001
            outcome = MetricOutcome(
                score=None,
                passed=False,
                reason=f"evaluator error: {type(exc).__name__}: {exc}",
                evidence={"error_type": type(exc).__name__},
                labels=["evaluator-error"],
            )
        scored.append((spec, outcome))
    return scored


def _eval_input(tc: TestCase, outcome: CaseOutcome) -> EvalInput:
    out = outcome.output
    assert out is not None
    return EvalInput(
        input=tc.input,
        expected_answer=tc.expected_answer,
        expected_answer_contains=list(tc.expected_answer_contains or []),
        expected_answer_regex=tc.expected_answer_regex,
        expected_context=list(tc.expected_context or []),
        answer=out.answer,
        retrieved_doc_ids=out.retrieved_doc_ids,
        citations=out.citations,
        latency_ms=outcome.latency_ms,
        input_tokens=out.input_tokens,
        output_tokens=out.output_tokens,
        model=out.model,
    )


async def _fail(session_factory: async_sessionmaker[AsyncSession], run_id: str, message: str) -> None:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is not None and run.status not in TERMINAL_STATUSES:
            run_service.transition(run, RunStatus.failed, error_message=message)
            await session.commit()


async def _start(session_factory: async_sessionmaker[AsyncSession], run_id: str) -> dict | None:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is None:
            log.warning("run %s not found; nothing to do", run_id)
            return None
        if run.status in TERMINAL_STATUSES:
            log.info("run %s is already %s; nothing to do", run_id, run.status.value)
            return None
        if run.status == RunStatus.pending:
            run_service.transition(run, RunStatus.running)
        else:
            log.warning("run %s was already running (previous attempt died); restarting it from scratch", run_id)
            result_ids = select(EvaluationResult.id).where(EvaluationResult.evaluation_run_id == run_id)
            await session.execute(delete(MetricScore).where(MetricScore.evaluation_result_id.in_(result_ids)))
            await session.execute(delete(EvaluationResult).where(EvaluationResult.evaluation_run_id == run_id))
        await session.commit()

        cases = list(
            await session.scalars(
                select(TestCase).where(TestCase.dataset_version_id == run.dataset_version_id).order_by(TestCase.case_key)
            )
        )
        return {
            "cases": cases,
            "evaluators": list(run.evaluators),
            "adapter_type": run.adapter_type,
            "adapter_target": run.adapter_target,
            "timeout": run.timeout_seconds or 10.0,
            "threshold": run.threshold,
            "max_latency_ms": run.max_latency_ms,
            "labels": run_service.run_labels(run),
        }


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    tc: TestCase,
    outcome: CaseOutcome,
    scored: list[tuple[EvaluatorSpec, MetricOutcome]],
    run_labels: list[str],
) -> None:
    passed = outcome.status == "ok" and not any(m.passed is False for _spec, m in scored)
    out = outcome.output
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        assert run is not None
        run_service.assert_accepts_results(run)
        result = EvaluationResult(
            evaluation_run_id=run_id,
            test_case_id=tc.id,
            output_answer=out.answer if out else None,
            retrieved_doc_ids=out.retrieved_doc_ids if out else [],
            citations=out.citations if out else [],
            input_tokens=out.input_tokens if out else None,
            output_tokens=out.output_tokens if out else None,
            model=out.model if out else None,
            latency_ms=outcome.latency_ms,
            status=ResultStatus(outcome.status),
            passed=passed,
            error_type=outcome.error_type,
            error_message=outcome.error_message,
        )
        session.add(result)
        await session.flush()
        for spec, m in scored:
            session.add(
                MetricScore(
                    evaluation_result_id=result.id,
                    evaluator_name=spec.name,
                    evaluator_version=spec.version,
                    score=m.score,
                    value=m.value,
                    unit=m.unit,
                    passed=m.passed,
                    reason=m.reason,
                    evidence=m.evidence,
                    labels=list(dict.fromkeys([*run_labels, *m.labels])),
                )
            )
        await session.commit()


async def _complete(session_factory: async_sessionmaker[AsyncSession], run_id: str) -> None:
    async with session_factory() as session:
        run = await run_service.load_run(session, run_id)
        assert run is not None
        aggregates = compute_aggregates(run_service.case_records(run.results))
        run.aggregates = aggregates
        run_service.transition(run, RunStatus.completed)
        await session.commit()


async def execute_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    pricing: Mapping[str, ModelPrice] | None = None,
) -> str:
    """Returns the run's final status (or a no-op reason) -- used as the arq job result."""
    try:
        started = await _start(session_factory, run_id)
        if started is None:
            return "skipped"
        try:
            specs = [resolve(key) for key in started["evaluators"]]
            adapter = build_adapter(started["adapter_type"], started["adapter_target"])
        except (AdapterLoadError, ValueError) as exc:
            await _fail(session_factory, run_id, f"run setup failed: {exc}")
            return "failed"

        config = EvalConfig(
            threshold=started["threshold"], max_latency_ms=started["max_latency_ms"], pricing=pricing or {}
        )
        try:
            for tc in started["cases"]:
                outcome = await invoke(adapter, tc.input, tc.case_key, started["timeout"])
                scored = score_case(specs, _eval_input(tc, outcome), config) if outcome.status == "ok" else []
                await _record(session_factory, run_id, tc, outcome, scored, started["labels"])
                log.info("run %s case %s -> %s (%.0f ms)", run_id, tc.case_key, outcome.status, outcome.latency_ms)
        finally:
            await adapter.aclose()

        await _complete(session_factory, run_id)
        log.info("run %s completed (%d case(s))", run_id, len(started["cases"]))
        return "completed"
    except asyncio.CancelledError:
        # arq job timeout or worker shutdown mid-run. Mark it failed (terminal,
        # visible) rather than leaving it `running` with half its results.
        await asyncio.shield(_fail(session_factory, run_id, "worker stopped or job timed out mid-run"))
        raise
    except Exception as exc:  # noqa: BLE001 - infrastructure failure (DB down, ...)
        log.exception("run %s failed", run_id)
        try:
            await _fail(session_factory, run_id, f"worker error: {type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            log.exception("could not mark run %s failed", run_id)
        return "failed"
