"""Failure replay: re-run one evaluated case, optionally with overrides.

    GET  /results/{result_id}/replay-options  what a replay of that result may override
    POST /replay                       create a pending replay and queue it for the worker (202)
    GET  /replays/{id}                 the replay, its outcome, spans, and the diff against the original
    GET  /results/{result_id}/replays  every replay of one result, newest first

Like runs, the API never executes anything. The worker records each run's
adapter declaration (agentforge_sdk.replay) with the run; POST /replay checks
the overrides against it and answers 400 with the accepted settings on a
mismatch. The worker checks again when the replay runs (the second check also
covers rules across settings, which can't be recorded, and runs from before
declarations were recorded). The original run and its result are only read.
"""

from __future__ import annotations

from typing import Any

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
from agentforge_core.schemas import ReplayCreate, ReplayOptionsOut, ReplayOut, ReplaySettingOut, ReplaySummaryOut
from agentforge_sdk.replay import OverrideError, ReplaySettings

router = APIRouter(tags=["replays"])


def _options(result: EvaluationResult, run: EvaluationRun, case_key: str) -> ReplayOptionsOut:
    recorded = run.replay_options
    common: dict[str, Any] = {
        "result_id": result.id,
        "run_id": run.id,
        "case_key": case_key,
        "adapter_type": run.adapter_type,
        "adapter_target": run.adapter_target,
    }
    if recorded is None:
        reason = (
            "this run didn't record its adapter's replay settings (it predates that); a replay can still be "
            "requested, and the worker checks any overrides when it runs"
        )
        if run.adapter_type is None:
            reason = "this run predates the worker (no adapter recorded); there's nothing to replay"
        elif run.adapter_type != "python":
            reason = "HTTP adapters can't declare replay settings (agentforge_sdk.replay); replay without overrides"
        return ReplayOptionsOut(
            **common, recorded=False, overrides_supported=False, reason=reason, settings=[], worker_checks=False
        )
    run_settings = run.adapter_settings or {}
    return ReplayOptionsOut(
        **common,
        recorded=True,
        overrides_supported=bool(recorded["overrides_supported"]),
        reason=recorded.get("reason"),
        # A setting the run set (its adapter_settings) defaults to the run's value: what a replay starts from.
        settings=[
            ReplaySettingOut.model_validate(
                {**d, "default": run_settings[d["name"]]} if d["name"] in run_settings else d
            )
            for d in recorded["settings"]
        ],
        worker_checks=bool(recorded.get("worker_checks")),
    )


async def _result_and_run(session: AsyncSession, result_id: str) -> tuple[EvaluationResult, EvaluationRun]:
    result = await session.get(EvaluationResult, result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"result '{result_id}' not found (use results[].id of a run)")
    run = await session.get(EvaluationRun, result.evaluation_run_id)
    assert run is not None
    return result, run


@router.get("/results/{result_id}/replay-options", response_model=ReplayOptionsOut)
async def get_replay_options(result_id: str, session: AsyncSession = Depends(get_session)) -> ReplayOptionsOut:
    """The settings a replay of this result may override (from the run's adapter declaration), with
    their types, allowed values and defaults; `overrides_supported` false (with a reason) when none."""
    result, run = await _result_and_run(session, result_id)
    test_case = await session.get(TestCase, result.test_case_id)
    assert test_case is not None
    return _options(result, run, test_case.case_key)


def _reject(message: str, options: ReplayOptionsOut) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={"message": message, "accepted_settings": [s.model_dump() for s in options.settings]},
    )


@router.post(
    "/replay",
    response_model=ReplayOut,
    status_code=202,
    responses={400: {"description": "Overrides the run's adapter doesn't accept: {message, accepted_settings}"}},
)
async def create_replay(
    payload: ReplayCreate,
    session: AsyncSession = Depends(get_session),
    queue: RunQueue = Depends(get_run_queue),
) -> ReplayOut:
    """Create a `pending` replay of one case result and enqueue it. 202: poll
    GET /replays/{id} until it's completed or failed."""
    result, run = await _result_and_run(session, payload.result_id)
    if run.status not in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409, detail=f"run {run.id} is still {run.status.value}; replay a result of a finished run"
        )
    if run.adapter_type is None or run.adapter_target is None:
        raise HTTPException(
            status_code=400, detail=f"run {run.id} predates the worker (no adapter recorded); there's nothing to replay"
        )
    test_case = await session.get(TestCase, result.test_case_id)
    assert test_case is not None
    options = _options(result, run, test_case.case_key)
    # An HTTP adapter can't take overrides even when its run predates recorded options.
    if payload.overrides and (options.recorded or run.adapter_type != "python"):
        if not options.overrides_supported:
            raise _reject(f"overrides rejected: {options.reason}", options)
        declared = ReplaySettings.from_description([s.model_dump() for s in options.settings])
        try:
            declared.validate(payload.overrides)
        except OverrideError as exc:
            raise _reject(f"overrides rejected: adapter '{run.adapter_target}': {exc}", options) from None

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
            adapter_settings=run.adapter_settings,
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
