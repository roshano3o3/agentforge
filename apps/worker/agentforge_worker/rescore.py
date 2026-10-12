"""Execute one re-score: score a finished run's stored outputs again (no adapter call).

Idempotent per re-score id: a finished one is left alone; one found `running` (a previous attempt
died) has its partial results cleared and starts over. For each stored result of the original run,
in case order:
  * ok      -> the case's evaluators (its dataset config, the re-score's pinned versions) on the stored
               answer, steps, tokens and latency -- runner.case_evaluators + runner.evaluate_one, the
               run's own code path -- and the new case verdict;
  * error / timeout -> stays failed with no scores, as in the run (there's no output to score).
Each case is committed as it's scored; aggregates (same function as runs) and the flip to completed
come last, since the triggers refuse writes to a finished re-score. No spans are recorded: nothing
runs but deterministic evaluators.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from agentforge_api.models.dataset import DatasetVersion
from agentforge_api.models.evaluation import EvaluationResult, EvaluationRun, ResultStatus
from agentforge_api.models.rescore import FINISHED_RESCORE_STATUSES, Rescore, RescoreMetricScore, RescoreResult
from agentforge_api.services import rescores as rescore_service
from agentforge_api.services import runs as run_service
from agentforge_evaluators import (
    CaseRecord,
    EvalConfig,
    EvalInput,
    EvaluatorSpec,
    MetricRecord,
    ModelPrice,
    TrajectoryStep,
    compute_aggregates,
)
from agentforge_worker import provenance
from agentforge_worker.runner import PinnedEvaluators, case_evaluators, evaluate_one

log = logging.getLogger("agentforge.worker")


class RescorePins(PinnedEvaluators):
    """The re-score's pinned versions. A case config that names an exact version (`name@1.0.0`) of an
    evaluator the re-score replaced gets the re-score's version: replacing is by name, so it must not be
    silently skipped as an unpinned key."""

    def __init__(self, keys: list[str], replaced: Mapping[str, str]) -> None:
        super().__init__(keys)
        self._replaced = set(replaced)

    def lookup(self, key: str) -> EvaluatorSpec | None:
        spec = super().lookup(key)
        name = key.partition("@")[0]
        if spec is None and "@" in key and name in self._replaced:
            return super().lookup(name)
        return spec


def stored_input(result: EvaluationResult) -> EvalInput:
    """The EvalInput the run built for this case, rebuilt from what it stored."""
    tc = result.test_case
    return EvalInput(
        input=tc.input,
        expected_answer=tc.expected_answer,
        expected_context=list(tc.expected_context or []),
        answer=result.output_answer or "",
        retrieved_doc_ids=list(result.retrieved_doc_ids or []),
        citations=list(result.citations or []),
        latency_ms=float(result.latency_ms),
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        model=result.model,
        steps=tuple(
            TrajectoryStep(
                index=s.step_index, kind=s.kind, name=s.name, args=dict(s.args or {}), result=s.result, error=s.error
            )
            for s in sorted(result.steps, key=lambda s: s.step_index)
        ),
        trajectory=tc.trajectory,
        safety=tc.safety,
    )


async def _fail(session_factory: async_sessionmaker[AsyncSession], rescore_id: str, message: str) -> None:
    async with session_factory() as session:
        rescore = await session.get(Rescore, rescore_id)
        if rescore is not None and rescore.status not in FINISHED_RESCORE_STATUSES:
            rescore_service.transition(rescore, "failed", error_message=message)
            await session.commit()


async def execute_rescore(
    session_factory: async_sessionmaker[AsyncSession],
    rescore_id: str,
    pricing: Mapping[str, ModelPrice] | None = None,
) -> str:
    """Returns the final status (or "skipped") -- the arq job result."""
    try:
        async with session_factory() as session:
            rescore = await session.get(Rescore, rescore_id)
            if rescore is None or rescore.status in FINISHED_RESCORE_STATUSES:
                return "skipped"
            run = await session.get(EvaluationRun, rescore.original_run_id)
            assert run is not None
            if rescore.status == "running":
                log.warning("re-score %s was already running (previous attempt died); starting over", rescore_id)
                await session.execute(delete(RescoreMetricScore).where(RescoreMetricScore.rescore_id == rescore_id))
                await session.execute(delete(RescoreResult).where(RescoreResult.rescore_id == rescore_id))
            else:
                rescore_service.transition(rescore, "running")
            prov = provenance.collect(run.adapter_type, run.adapter_target)
            rescore.code_version = prov.get("code_version")
            rescore.code_sha256 = prov.get("code_sha256")
            await session.commit()
            evaluators = list(rescore.evaluators)
            replaced = dict(rescore.replaced)
            version = await session.get(DatasetVersion, run.dataset_version_id)
            assert version is not None
            default = version.default_evaluators
            config = EvalConfig(threshold=run.threshold, max_latency_ms=run.max_latency_ms, pricing=pricing or {})
            labels = run_service.run_labels(run)
            results = list(
                await session.scalars(
                    select(EvaluationResult)
                    .where(EvaluationResult.evaluation_run_id == run.id)
                    .options(selectinload(EvaluationResult.test_case), selectinload(EvaluationResult.steps))
                )
            )
        results.sort(key=lambda r: r.test_case.case_key)
        pinned = RescorePins(evaluators, replaced)
        records: list[CaseRecord] = []
        for result in results:
            scored = []
            if result.status == ResultStatus.ok:
                for spec, params in case_evaluators(pinned, default, result.test_case):
                    outcome, _exc = evaluate_one(spec, params, stored_input(result), config)
                    scored.append((spec, outcome))
            passed = result.status == ResultStatus.ok and not any(m.passed is False for _s, m in scored)
            async with session_factory() as session:
                rescore = await session.get(Rescore, rescore_id)
                assert rescore is not None
                rescore_service.assert_accepts_outcome(rescore)
                row = RescoreResult(rescore_id=rescore_id, original_result_id=result.id, passed=passed)
                session.add(row)
                await session.flush()
                for position, (spec, m) in enumerate(scored):
                    session.add(
                        RescoreMetricScore(
                            rescore_id=rescore_id,
                            rescore_result_id=row.id,
                            position=position,
                            evaluator_name=spec.name,
                            evaluator_version=spec.version,
                            score=m.score,
                            value=m.value,
                            unit=m.unit,
                            passed=m.passed,
                            reason=m.reason,
                            evidence=m.evidence,
                            labels=list(dict.fromkeys([*labels, *m.labels])),
                        )
                    )
                await session.commit()
            records.append(
                CaseRecord(
                    status=result.status.value,
                    passed=passed,
                    latency_ms=float(result.latency_ms),
                    metrics=[
                        MetricRecord(
                            evaluator_name=spec.name,
                            evaluator_version=spec.version,
                            score=m.score,
                            value=m.value,
                            unit=m.unit,
                            passed=m.passed,
                            reason=m.reason,
                        )  # fmt: skip
                        for spec, m in scored
                    ],
                    category=(result.test_case.safety or {}).get("category"),
                )
            )
        async with session_factory() as session:
            rescore = await session.get(Rescore, rescore_id)
            assert rescore is not None
            rescore.aggregates = compute_aggregates(records)
            rescore_service.transition(rescore, "completed")
            await session.commit()
        log.info("re-score %s of run %s completed (%d case(s))", rescore_id, run.id, len(records))
        return "completed"
    except Exception as exc:  # noqa: BLE001 - infrastructure failure, or an unresolvable evaluator
        log.exception("re-score %s failed", rescore_id)
        await _fail(session_factory, rescore_id, f"worker error: {type(exc).__name__}: {exc}")
        return "failed"
