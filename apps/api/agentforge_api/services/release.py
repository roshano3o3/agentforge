"""Baselines, regression reports and release decisions, over persisted runs.

The arithmetic is agentforge_evaluators.release (pure functions); this module
only loads finished runs into `RunSnapshot`s and persists the outcomes.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentforge_api.models.application import Application, ApplicationVersion
from agentforge_api.models.dataset import Dataset, DatasetVersion
from agentforge_api.models.evaluation import EvaluationRun, RunStatus
from agentforge_api.models.release import Baseline, ReleaseDecision
from agentforge_api.services import runs as run_service
from agentforge_core.schemas import BaselineOut, GateCheckOut, ReleaseDecisionOut
from agentforge_evaluators import CaseOutcome, RunSnapshot, compute_aggregates


async def completed_run(session: AsyncSession, run_id: str, role: str) -> EvaluationRun:
    """The run with its results, or 404 / 400 (only completed runs compare)."""
    run = await run_service.load_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"{role} run '{run_id}' not found")
    if run.status != RunStatus.completed:
        raise HTTPException(
            status_code=400,
            detail=f"{role} run '{run_id}' is {run.status.value}; only completed runs can be compared",
        )
    return run


def snapshot(run: EvaluationRun) -> RunSnapshot:
    aggregates = run.aggregates or compute_aggregates(run_service.case_records(run.results))
    return RunSnapshot(
        run_id=run.id,
        dataset_version_id=run.dataset_version_id,
        aggregates=aggregates,
        cases={
            r.test_case.case_key: CaseOutcome(
                status=r.status.value,
                passed=bool(r.passed),
                tags=tuple(r.test_case.tags or []),
                failed_evaluators=tuple(sorted(m.evaluator_name for m in r.metrics if m.passed is False)),
            )
            for r in run.results
        },
    )


async def baseline_out(session: AsyncSession, row: Baseline) -> BaselineOut:
    run = await session.get(EvaluationRun, row.run_id)
    assert run is not None
    app = await session.get(Application, row.application_id)
    app_version = await session.get(ApplicationVersion, run.application_version_id)
    dataset_version = await session.get(DatasetVersion, run.dataset_version_id)
    assert app and app_version and dataset_version
    dataset = await session.get(Dataset, dataset_version.dataset_id)
    assert dataset
    return BaselineOut(
        application_id=app.id,
        application_name=app.name,
        environment=row.environment,
        run_id=run.id,
        set_at=row.set_at,
        application_version=app_version.version,
        dataset_name=dataset.name,
        dataset_version=dataset_version.version,
        pass_rate=(run.aggregates or {}).get("pass_rate"),
    )


async def resolve_baseline(session: AsyncSession, candidate: EvaluationRun, ref: str) -> EvaluationRun:
    """`ref` is a run id, or an environment whose baseline pointer (for the
    candidate's application) names the run."""
    if await session.get(EvaluationRun, ref) is not None:
        return await completed_run(session, ref, "baseline")
    pointer = await session.scalar(
        select(Baseline).where(Baseline.application_id == candidate.application_id, Baseline.environment == ref)
    )
    if pointer is None:
        app = await session.get(Application, candidate.application_id)
        name = app.name if app else candidate.application_id
        raise HTTPException(
            status_code=404,
            detail=f"no baseline set for application '{name}' in environment '{ref}' "
            f"(set one with: agentforge baseline set <run_id> --env {ref})",
        )
    return await completed_run(session, pointer.run_id, "baseline")


async def decision_out(session: AsyncSession, row: ReleaseDecision) -> ReleaseDecisionOut:
    app = await session.get(Application, row.application_id)
    assert app is not None
    return ReleaseDecisionOut(
        id=row.id,
        application_id=row.application_id,
        application_name=app.name,
        candidate_run_id=row.candidate_run_id,
        baseline_run_id=row.baseline_run_id,
        baseline_ref=row.baseline_ref,
        policy=row.policy,
        passed=row.passed,
        checks=[GateCheckOut.model_validate(c) for c in row.checks],
        regression=row.regression,
        created_at=row.created_at,
    )
