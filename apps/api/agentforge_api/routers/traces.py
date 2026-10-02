"""Stored traces: the span tree for one evaluated case.

A test case is evaluated once per run, so a case's trace is addressed by its
**result id** in that run (`results[].id` in GET /runs/{id}). The tree
contains the run-level spans above it (the API's run creation, the worker's
run) and everything recorded under the case. Spans are what the API, the
worker and the agent recorded and stored -- redacted of PII unless
AGENTFORGE_TRACE_REDACTION=off -- nothing is reconstructed or inferred here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentforge_api.db.base import get_session
from agentforge_api.models.dataset import TestCase
from agentforge_api.models.evaluation import AgentStep, EvaluationResult
from agentforge_api.models.trace import TraceSpan
from agentforge_core.schemas import TraceOut, TraceSpanOut

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get(
    "/{result_id}",
    response_model=TraceOut,
    summary="Span tree for one evaluated case",
    responses={404: {"description": "No such result, or no spans were stored for it"}},
)
async def get_trace(
    result_id: str = Path(
        description="The case's result id in a run (`results[].id` from GET /runs/{run_id}), "
        "not the test case id: a test case is evaluated once per run."
    ),
    session: AsyncSession = Depends(get_session),
) -> TraceOut:
    """The case's stored spans as a tree, with the run-level spans above it and
    each span's linked agent step (`step_index`)."""
    result = await session.get(EvaluationResult, result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"result '{result_id}' not found (use results[].id of a run)")
    case_spans = list(await session.scalars(select(TraceSpan).where(TraceSpan.evaluation_result_id == result_id)))
    if not case_spans:
        raise HTTPException(
            status_code=404,
            detail=f"no spans stored for result '{result_id}' (tracing was off for its run, "
            "or the run predates tracing)",
        )
    trace_id = case_spans[0].trace_id
    run_spans = list(
        await session.scalars(
            select(TraceSpan).where(
                TraceSpan.trace_id == trace_id,
                TraceSpan.run_id == result.evaluation_run_id,
                TraceSpan.evaluation_result_id.is_(None),
            )
        )
    )
    steps = {
        s.span_id: s.step_index
        for s in await session.scalars(
            select(AgentStep).where(AgentStep.evaluation_result_id == result_id, AgentStep.span_id.is_not(None))
        )
    }
    test_case = await session.get(TestCase, result.test_case_id)

    nodes = {s.span_id: _node(s, steps.get(s.span_id)) for s in [*run_spans, *case_spans]}
    roots: list[TraceSpanOut] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_span_id or "")
        (parent.children if parent is not None else roots).append(node)
    for node in nodes.values():
        node.children.sort(key=lambda n: (n.start_time, n.name))
    roots.sort(key=lambda n: n.start_time)
    return TraceOut(
        result_id=result_id,
        case_key=test_case.case_key if test_case else "",
        run_id=result.evaluation_run_id,
        trace_id=trace_id,
        span_count=len(nodes),
        spans=roots,
    )


def _node(s: TraceSpan, step_index: int | None) -> TraceSpanOut:
    return TraceSpanOut(
        span_id=s.span_id,
        parent_span_id=s.parent_span_id,
        name=s.name,
        kind=s.kind,
        service=s.service,
        start_time=s.start_time,
        end_time=s.end_time,
        duration_ms=s.duration_ms,
        attributes=s.attributes,
        status_code=s.status_code,  # type: ignore[arg-type]  # CHECK-constrained to the Literal values
        status_message=s.status_message,
        events=s.events,
        step_index=step_index,
        children=[],
    )
