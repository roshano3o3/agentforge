"""Baselines, regression comparison, and release-gate decisions.

Every number here is computed by the API from persisted, completed runs.
Clients set pointers and supply a policy; they never submit scores, deltas,
or verdicts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentforge_api.db.base import get_session
from agentforge_api.models.application import Application
from agentforge_api.models.release import Baseline, ReleaseDecision
from agentforge_api.services import release as release_service
from agentforge_core.schemas import (
    BaselineOut,
    BaselineSet,
    RegressionOut,
    ReleaseDecisionCreate,
    ReleaseDecisionOut,
)
from agentforge_evaluators import PolicyError, RegressionError, compare, evaluate_policy, parse_policy

router = APIRouter(tags=["release"])


# -- baselines ---------------------------------------------------------------------


@router.put("/baselines", response_model=BaselineOut)
async def set_baseline(payload: BaselineSet, session: AsyncSession = Depends(get_session)) -> BaselineOut:
    """Point (the run's application, environment) at a completed run. A
    metadata write: the pointer row is created or re-pointed; nothing else
    changes."""
    run = await release_service.completed_run(session, payload.run_id, "baseline")
    row = await session.scalar(
        select(Baseline).where(
            Baseline.application_id == run.application_id, Baseline.environment == payload.environment
        )
    )
    if row is None:
        row = Baseline(application_id=run.application_id, environment=payload.environment, run_id=run.id)
        session.add(row)
    else:
        row.run_id = run.id
        row.set_at = datetime.now(UTC)
    await session.commit()
    return await release_service.baseline_out(session, row)


@router.get("/baselines", response_model=list[BaselineOut])
async def list_baselines(
    application: str | None = Query(None, description="Application name filter"),
    environment: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> list[BaselineOut]:
    query = select(Baseline).join(Application, Application.id == Baseline.application_id)
    if application is not None:
        query = query.where(Application.name == application)
    if environment is not None:
        query = query.where(Baseline.environment == environment)
    rows = await session.scalars(query.order_by(Application.name, Baseline.environment))
    return [await release_service.baseline_out(session, row) for row in rows]


# -- regression ----------------------------------------------------------------------


@router.get("/regression", response_model=RegressionOut)
async def regression(baseline_run_id: str, candidate_run_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Compare two completed runs of the same dataset version (400 otherwise)."""
    baseline = await release_service.completed_run(session, baseline_run_id, "baseline")
    candidate = await release_service.completed_run(session, candidate_run_id, "candidate")
    try:
        return compare(release_service.snapshot(baseline), release_service.snapshot(candidate))
    except RegressionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# -- release decisions ---------------------------------------------------------------


@router.post("/release-decisions", response_model=ReleaseDecisionOut, status_code=201)
async def create_release_decision(
    payload: ReleaseDecisionCreate, session: AsyncSession = Depends(get_session)
) -> ReleaseDecisionOut:
    """Run the release gate: evaluate every check of `policy` for the
    candidate against the baseline and persist the (immutable) decision."""
    try:
        policy = parse_policy(payload.policy)
    except PolicyError as exc:
        raise HTTPException(status_code=422, detail=f"invalid release policy: {exc}") from exc
    candidate = await release_service.completed_run(session, payload.candidate_run_id, "candidate")
    baseline = await release_service.resolve_baseline(session, candidate, payload.baseline)
    base_snap, cand_snap = release_service.snapshot(baseline), release_service.snapshot(candidate)
    try:
        report = compare(base_snap, cand_snap)
        checks = evaluate_policy(policy, base_snap, cand_snap)
    except RegressionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = ReleaseDecision(
        application_id=candidate.application_id,
        candidate_run_id=candidate.id,
        baseline_run_id=baseline.id,
        baseline_ref=payload.baseline,
        policy=policy.to_dict(),
        passed=all(c["passed"] for c in checks),
        checks=checks,
        regression=report,
    )
    session.add(row)
    await session.commit()
    return await release_service.decision_out(session, row)


@router.get("/release-decisions/{decision_id}", response_model=ReleaseDecisionOut)
async def get_release_decision(decision_id: str, session: AsyncSession = Depends(get_session)) -> ReleaseDecisionOut:
    row = await session.get(ReleaseDecision, decision_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"release decision '{decision_id}' not found")
    return await release_service.decision_out(session, row)


@router.get("/release-decisions", response_model=list[ReleaseDecisionOut])
async def list_release_decisions(
    candidate_run_id: str | None = None, session: AsyncSession = Depends(get_session)
) -> list[ReleaseDecisionOut]:
    query = select(ReleaseDecision).order_by(ReleaseDecision.created_at.desc())
    if candidate_run_id is not None:
        query = query.where(ReleaseDecision.candidate_run_id == candidate_run_id)
    return [await release_service.decision_out(session, row) for row in await session.scalars(query)]
