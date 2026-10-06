"""Failure replay: re-run one evaluated case, optionally with overrides.

    POST /replay                       create a pending replay and queue it for the worker (202)
    GET  /replays/{id}                 the replay, its outcome, spans, and the diff against the original
    GET  /results/{result_id}/replays  every replay of one result, newest first

Like runs, the API never executes anything: it validates what it can (the
result exists, its run is finished, the overrides' shape), copies the run's
settings onto the replay, and enqueues it. Whether the adapter accepts the
overrides is checked by the worker, against the adapter's own declaration
(agentforge_sdk.replay) -- a mismatch fails the replay with that message.
The original run and its result are only read.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from opentelemetry import trace
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentforge_api import tracing
from agentforge_api.db.base import get_session
from agentforge_api.models.dataset import TestCase
from agentforge_api.models.evaluation import TERMINAL_STATUSES, EvaluationResult, EvaluationRun
from agentforge_api.models.replay import Replay, ReplaySpan
from agentforge_api.queue import QueueUnavailableError, RunQueue, get_run_queue
from agentforge_api.services import replays as replay_service
from agentforge_core.schemas import ReplayCreate, ReplayOut, ReplaySummaryOut

router = APIRouter(tags=["replays"])


@router.post("/replay", response_model=ReplayOut, status_code=202)
async def create_replay(
    payload: ReplayCreate,
    session: AsyncSession = Depends(get_session),
    queue: RunQueue = Depends(get_run_queue),
) -> ReplayOut:
    """Create a `pending` replay of one case result and enqueue it. 202: poll
    GET /replays/{id} until it's completed or failed."""
    result = await session.get(EvaluationResult, payload.result_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"result '{payload.result_id}' not found (use results[].id of a run)"
        )
    run = await session.get(EvaluationRun, result.evaluation_run_id)
    assert run is not None
    if run.status not in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"run {run.id} is still {run.status.value}; replay a result of a finished run"
        )
    if run.adapter_type is None or run.adapter_target is None:
        raise HTTPException(
            status_code=400, detail=f"run {run.id} predates the worker (no adapter recorded); there's nothing to replay"
        )
    if payload.overrides and run.adapter_type != "python":
        raise HTTPException(
            status_code=400,
            detail=(
                f"overrides rejected: run {run.id} used an {run.adapter_type} adapter, which can't declare replay "
                f"settings (got: {', '.join(sorted(payload.overrides))}); replay it without overrides"
            ),
        )
    test_case = await session.get(TestCase, result.test_case_id)
    assert test_case is not None

    span = tracing.tracer().start_span(
        "agentforge.replay.create",
        attributes=tracing.attrs(
            {
                "agentforge.component": "api",
                "agentforge.replay.original_result_id": result.id,
                "agentforge.run.id": run.id,
                "agentforge.case.key": test_case.case_key,
                "agentforge.replay.overrides": payload.overrides or None,
            }
        ),
    )
    try:
        replay = Replay(
            original_result_id=result.id,
            original_run_id=run.id,
            test_case_id=result.test_case_id,
            dataset_version_id=run.dataset_version_id,
            adapter_type=run.adapter_type,
            adapter_target=run.adapter_target,
            evaluators=list(run.evaluators),
            provider_type=run.provider_type,
            threshold=run.threshold,
            max_latency_ms=run.max_latency_ms,
            timeout_seconds=run.timeout_seconds,
            overrides=payload.overrides,
            status="pending",
        )
        session.add(replay)
        await session.flush()
        span.set_attribute("agentforge.replay.id", replay.id)
    except BaseException as exc:
        tracing.error(span, f"{type(exc).__name__}: {exc}", exc)
        raise
    finally:
        span.end()
        ctx = span.get_span_context()
        collector = tracing.collector()
        claimed = collector.take_subtree(ctx.trace_id, ctx.span_id) if collector and ctx.is_valid else []
        if collector is not None and ctx.is_valid:
            collector.discard(ctx.trace_id)
    if claimed:
        replay.trace_id = tracing.hex_trace_id(claimed[0])
        await session.execute(insert(ReplaySpan), [{**tracing.row_values(s), "replay_id": replay.id} for s in claimed])
    await session.commit()

    try:
        await queue.enqueue_replay(replay.id, tracing.inject(trace.set_span_in_context(span)))
    except QueueUnavailableError as exc:
        replay_service.transition(replay, "failed", error_message=f"not executed: {exc}")
        await session.commit()
        raise HTTPException(
            status_code=503,
            detail=f"replay {replay.id} was created but could not be queued, and is marked failed: {exc}",
        ) from exc

    loaded = await replay_service.load_replay(session, replay.id)
    assert loaded is not None
    return await replay_service.to_out(session, loaded)


@router.get("/replays/{replay_id}", response_model=ReplayOut)
async def get_replay(replay_id: str, session: AsyncSession = Depends(get_session)) -> ReplayOut:
    replay = await replay_service.load_replay(session, replay_id)
    if replay is None:
        raise HTTPException(status_code=404, detail=f"replay '{replay_id}' not found")
    return await replay_service.to_out(session, replay)


@router.get("/results/{result_id}/replays", response_model=list[ReplaySummaryOut])
async def list_result_replays(result_id: str, session: AsyncSession = Depends(get_session)) -> list[ReplaySummaryOut]:
    if await session.get(EvaluationResult, result_id) is None:
        raise HTTPException(status_code=404, detail=f"result '{result_id}' not found (use results[].id of a run)")
    ids = list(
        await session.scalars(
            select(Replay.id).where(Replay.original_result_id == result_id).order_by(Replay.created_at.desc())
        )
    )
    out = []
    for replay_id in ids:
        replay = await replay_service.load_replay(session, replay_id)
        assert replay is not None
        out.append(await replay_service.to_summary(session, replay))
    return out
