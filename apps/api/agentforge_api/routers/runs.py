from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentforge_api.db.base import get_session
from agentforge_api.models.application import Application, ApplicationVersion
from agentforge_api.models.dataset import DatasetVersion, DatasetVersionStatus, TestCase
from agentforge_api.models.evaluation import EvaluationRun, RunStatus
from agentforge_api.queue import QueueUnavailableError, RunQueue, get_run_queue
from agentforge_api.services import runs as run_service
from agentforge_core.schemas import EvaluationRunCreate, EvaluationRunOut, EvaluationRunSummaryOut
from agentforge_evaluators import DEFAULT_EVALUATORS, UnknownEvaluatorError, referenced_keys, resolve

router = APIRouter(prefix="/runs", tags=["runs"])


async def _pin_evaluators(
    requested: list[str] | None, dataset_version: DatasetVersion, session: AsyncSession
) -> list[str]:
    """Resolve the run's evaluators now and store them pinned as
    name@version, so a later evaluator release never changes what this run
    means. Unknown names are a 400, before anything is persisted.

    Default: exactly the evaluators the dataset version's configs reference
    (all registered ones for versions without a default config). An explicit
    list is a filter: per case, only configured evaluators that are also in
    the list run.
    """
    if requested:
        specs = requested
    else:
        case_configs: list[dict | None] = list(
            await session.scalars(select(TestCase.evaluators).where(TestCase.dataset_version_id == dataset_version.id))
        )
        referenced = referenced_keys(dataset_version.default_evaluators, case_configs)
        specs = DEFAULT_EVALUATORS if referenced is None else referenced
        if not specs:
            raise HTTPException(
                status_code=400,
                detail="this dataset version's evaluator config applies no evaluators to any case",
            )
    pinned: list[str] = []
    for spec in specs:
        try:
            key = resolve(spec).key
        except UnknownEvaluatorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if key not in pinned:
            pinned.append(key)
    return pinned


@router.post("", response_model=EvaluationRunOut, status_code=202)
async def create_run(
    payload: EvaluationRunCreate,
    session: AsyncSession = Depends(get_session),
    queue: RunQueue = Depends(get_run_queue),
) -> EvaluationRunOut:
    """Create a `pending` run and enqueue it for the worker. 202: the run
    executes asynchronously -- poll GET /runs/{id} until it's completed or
    failed."""
    application = await session.get(Application, payload.application_id)
    if not application:
        raise HTTPException(status_code=404, detail=f"application '{payload.application_id}' not found")
    app_version = await session.get(ApplicationVersion, payload.application_version_id)
    if not app_version or app_version.application_id != application.id:
        raise HTTPException(
            status_code=404,
            detail=f"application version '{payload.application_version_id}' not found for this application",
        )
    dataset_version = await session.get(DatasetVersion, payload.dataset_version_id)
    if not dataset_version:
        raise HTTPException(status_code=404, detail=f"dataset version '{payload.dataset_version_id}' not found")
    if dataset_version.status != DatasetVersionStatus.published:
        raise HTTPException(
            status_code=400,
            detail=(
                f"dataset version '{payload.dataset_version_id}' is a draft (not published) -- "
                "publish it before evaluating against it, so the run's dataset reference stays "
                "reproducible."
            ),
        )

    run = EvaluationRun(
        application_id=application.id,
        application_version_id=app_version.id,
        dataset_version_id=dataset_version.id,
        provider_type=payload.provider_type,
        evaluators=await _pin_evaluators(payload.evaluators, dataset_version, session),
        adapter_type=payload.adapter.type,
        adapter_target=payload.adapter.target,
        timeout_seconds=payload.timeout_seconds,
        max_latency_ms=payload.max_latency_ms,
        environment=payload.environment,
        threshold=payload.threshold,
        git_commit_sha=payload.git_commit_sha,
        status=RunStatus.pending,
    )
    session.add(run)
    await session.commit()

    try:
        await queue.enqueue_run(run.id)
    except QueueUnavailableError as exc:
        # Never leave a run pending forever with no job behind it.
        run_service.transition(run, RunStatus.failed, error_message=f"not executed: {exc}")
        await session.commit()
        raise HTTPException(
            status_code=503, detail=f"run {run.id} was created but could not be queued, and is marked failed: {exc}"
        ) from exc

    loaded = await run_service.load_run(session, run.id)
    assert loaded is not None
    return await run_service.to_detail(session, loaded)


@router.get("/{run_id}", response_model=EvaluationRunOut)
async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> EvaluationRunOut:
    run = await run_service.load_run(session, run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
    return await run_service.to_detail(session, run)


@router.get("", response_model=list[EvaluationRunSummaryOut])
async def list_runs(session: AsyncSession = Depends(get_session)) -> list[EvaluationRunSummaryOut]:
    rows = await session.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc()))
    return [await run_service.to_summary(session, run) for run in rows]
