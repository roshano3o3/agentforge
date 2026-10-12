"""Re-scores: score a finished run's stored outputs again with other evaluator versions.

    POST /runs/{run_id}/rescores   create a pending re-score and queue it for the worker (202)
    GET  /rescores/{id}            the re-score: its evaluators, case verdicts, aggregates, what changed
    GET  /runs/{run_id}/rescores   every re-score of one run, newest first

No adapter is called and the original run is only read. The worker runs the evaluators (the API
never does): the run's pinned versions with the requested ones swapped in by name, on each stored
result, with the frozen test case's config -- the same code path as the run.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentforge_api.db.base import get_session
from agentforge_api.models.evaluation import EvaluationRun, RunStatus
from agentforge_api.models.rescore import Rescore
from agentforge_api.queue import QueueUnavailableError, RunQueue, get_run_queue
from agentforge_api.services import rescores as rescore_service
from agentforge_core.schemas import RescoreCreate, RescoreOut, RescoreSummaryOut
from agentforge_evaluators import UnknownEvaluatorError

router = APIRouter(tags=["rescores"])


@router.post("/runs/{run_id}/rescores", response_model=RescoreOut, status_code=202)
async def create_rescore(
    run_id: str,
    payload: RescoreCreate,
    session: AsyncSession = Depends(get_session),
    queue: RunQueue = Depends(get_run_queue),
) -> RescoreOut:
    """202 and a pending re-score; poll GET /rescores/{id}. 404 unknown run, 409 run not completed,
    400 unknown evaluator."""
    run = await session.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    if run.status != RunStatus.completed:
        raise HTTPException(
            status_code=409, detail=f"run {run_id} is {run.status.value}; only a completed run can be re-scored"
        )
    try:
        evaluators, replaced = rescore_service.swap_versions(list(run.evaluators), payload.evaluators)
    except UnknownEvaluatorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    rescore = Rescore(
        original_run_id=run.id,
        requested_evaluators=list(payload.evaluators),
        evaluators=evaluators,
        replaced=replaced,
        status="pending",
    )
    session.add(rescore)
    await session.commit()
    try:
        await queue.enqueue_rescore(rescore.id)
    except QueueUnavailableError as exc:
        rescore_service.transition(rescore, "failed", error_message=f"not executed: {exc}")
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail=f"re-score {rescore.id} was created but could not be queued, and is marked failed: {exc}",
        ) from exc
    loaded = await rescore_service.load_rescore(session, rescore.id)
    assert loaded is not None
    return await rescore_service.to_out(session, loaded)


@router.get("/rescores/{rescore_id}", response_model=RescoreOut)
async def get_rescore(rescore_id: str, session: AsyncSession = Depends(get_session)) -> RescoreOut:
    rescore = await rescore_service.load_rescore(session, rescore_id)
    if rescore is None:
        raise HTTPException(status_code=404, detail=f"re-score '{rescore_id}' not found")
    return await rescore_service.to_out(session, rescore)


@router.get("/runs/{run_id}/rescores", response_model=list[RescoreSummaryOut])
async def list_run_rescores(run_id: str, session: AsyncSession = Depends(get_session)) -> list[RescoreSummaryOut]:
    if await session.get(EvaluationRun, run_id) is None:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    ids = list(
        await session.scalars(
            select(Rescore.id).where(Rescore.original_run_id == run_id).order_by(Rescore.created_at.desc())
        )
    )
    out = []
    for rescore_id in ids:
        rescore = await rescore_service.load_rescore(session, rescore_id)
        assert rescore is not None
        out.append(await rescore_service.to_summary(session, rescore))
    return out
