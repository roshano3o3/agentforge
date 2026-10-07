"""Execute one replay: pending -> running -> completed | failed.

A replay re-runs one case of a finished run with the run's own adapter,
pinned evaluator versions, per-case evaluator config, threshold, latency
budget and timeout, against the same (immutable) dataset version -- so the
same input and scenario. Only the adapter's `overrides` differ, and only if
the adapter declared those settings: `check_overrides` validates them before
anything runs, and a mismatch fails the replay with the reason.

The case goes through the runner's own `_run_case` (agent call, evaluators,
spans), so a replay without overrides executes exactly what the run did.
Its outcome goes to the replay tables; the original run is never written.

Tracing: `agentforge.replay` continues the trace the API started for the
replay, and the case's span tree sits under it. All of it is stored in
`replay_spans`, redacted like every stored span.

Idempotent per replay id, like runs: a finished replay is left alone, and a
replay found `running` (the worker died mid-way) has its partial outcome
cleared and starts over.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_api import tracing
from agentforge_api.models.dataset import DatasetVersion, TestCase, content_hash_of
from agentforge_api.models.replay import FINISHED_REPLAY_STATUSES, Replay, ReplayMetricScore, ReplaySpan, ReplayStep
from agentforge_api.services import replays as replay_service
from agentforge_core.schemas import FIXTURE_BASED_LABEL, LOCAL_DETERMINISTIC
from agentforge_evaluators import EvalConfig, EvaluatorSpec, MetricOutcome, ModelPrice
from agentforge_sdk.replay import OverrideError
from agentforge_worker import provenance
from agentforge_worker.adapters import AdapterLoadError, CaseOutcome, build_adapter, check_overrides
from agentforge_worker.runner import PinnedEvaluators, _claim, _run_case

log = logging.getLogger("agentforge.worker")


async def _store_spans(session: AsyncSession, replay_id: str, rows: list[dict[str, Any]]) -> None:
    if rows:
        await session.execute(insert(ReplaySpan), [{**v, "replay_id": replay_id} for v in rows])


async def _fail(
    session_factory: async_sessionmaker[AsyncSession],
    replay_id: str,
    message: str,
    spans: list[dict[str, Any]] | None = None,
) -> None:
    async with session_factory() as session:
        replay = await session.get(Replay, replay_id)
        if replay is not None and replay.status not in FINISHED_REPLAY_STATUSES:
            await _store_spans(session, replay_id, spans or [])
            replay_service.transition(replay, "failed", error_message=message)
            await session.commit()


async def _start(session_factory: async_sessionmaker[AsyncSession], replay_id: str) -> dict | None:
    async with session_factory() as session:
        replay = await session.get(Replay, replay_id)
        if replay is None:
            log.warning("replay %s not found; nothing to do", replay_id)
            return None
        if replay.status in FINISHED_REPLAY_STATUSES:
            log.info("replay %s is already %s; nothing to do", replay_id, replay.status)
            return None
        for key, value in provenance.collect(replay.adapter_type, replay.adapter_target).items():
            setattr(replay, key, value)
        if replay.status == "pending":
            replay_service.transition(replay, "running")
        else:
            log.warning("replay %s was already running (previous attempt died); starting it over", replay_id)
            await session.execute(delete(ReplayStep).where(ReplayStep.replay_id == replay_id))
            await session.execute(delete(ReplayMetricScore).where(ReplayMetricScore.replay_id == replay_id))
            # The worker's spans from the dead attempt; the API's creation span stays.
            await session.execute(
                delete(ReplaySpan).where(ReplaySpan.replay_id == replay_id, ReplaySpan.service != "agentforge-api")
            )
        await session.commit()

        tc = await session.get(TestCase, replay.test_case_id)
        version = await session.get(DatasetVersion, replay.dataset_version_id)
        assert tc is not None and version is not None
        cases = list(await session.scalars(select(TestCase).where(TestCase.dataset_version_id == version.id)))
        return {
            "case": tc,
            "overrides": dict(replay.overrides or {}),
            "evaluators": list(replay.evaluators),
            "original_result_id": replay.original_result_id,
            "labels": [FIXTURE_BASED_LABEL] if replay.provider_type == LOCAL_DETERMINISTIC else [],
            "threshold": replay.threshold,
            "max_latency_ms": replay.max_latency_ms,
            # The same keys the runner's _run_case reads for a run.
            "dataset_content_hash": content_hash_of(version.default_evaluators, cases),
            "dataset_version_id": version.id,
            "default_evaluators": version.default_evaluators,
            "adapter_type": replay.adapter_type,
            "adapter_target": replay.adapter_target,
            "timeout": replay.timeout_seconds or 10.0,
        }


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    replay_id: str,
    started: dict,
    outcome: CaseOutcome,
    scored: list[tuple[EvaluatorSpec, MetricOutcome]],
    spans: list[dict[str, Any]],
) -> None:
    """The replayed case's outcome, steps, scores and spans, in one transaction (still running)."""
    out = outcome.output
    async with session_factory() as session:
        replay = await session.get(Replay, replay_id)
        assert replay is not None
        replay_service.assert_accepts_outcome(replay)
        replay.dataset_content_hash = started["dataset_content_hash"]
        replay.result_status = outcome.status
        replay.passed = outcome.status == "ok" and not any(m.passed is False for _spec, m in scored)
        replay.output_answer = out.answer if out else None
        replay.retrieved_doc_ids = out.retrieved_doc_ids if out else []
        replay.citations = out.citations if out else []
        replay.input_tokens = out.input_tokens if out else None
        replay.output_tokens = out.output_tokens if out else None
        replay.model = out.model if out else None
        replay.latency_ms = outcome.latency_ms
        replay.error_type = outcome.error_type
        replay.case_error_message = outcome.error_message
        for i, step in enumerate(out.steps if out else [], start=1):
            session.add(
                ReplayStep(
                    replay_id=replay_id,
                    step_index=i,
                    kind=step.kind,
                    name=step.name,
                    args=dict(step.args),
                    result=step.result,
                    error=step.error,
                    retrieved_doc_ids=list(step.retrieved_doc_ids),
                    output=step.output,
                    duration_ms=step.duration_ms,
                    span_id=step.span_id,
                )
            )
        for position, (spec, m) in enumerate(scored):
            session.add(
                ReplayMetricScore(
                    replay_id=replay_id,
                    position=position,
                    evaluator_name=spec.name,
                    evaluator_version=spec.version,
                    score=m.score,
                    value=m.value,
                    unit=m.unit,
                    passed=m.passed,
                    reason=m.reason,
                    evidence=m.evidence,
                    labels=list(dict.fromkeys([*started["labels"], *m.labels])),
                )
            )
        await session.flush()
        await _store_spans(session, replay_id, spans)
        await session.commit()


async def _complete(session_factory: async_sessionmaker[AsyncSession], replay_id: str, spans: list[dict]) -> None:
    async with session_factory() as session:
        replay = await session.get(Replay, replay_id)
        assert replay is not None
        # Span rows go in before the status flip: once completed, the trigger refuses them.
        await _store_spans(session, replay_id, spans)
        replay_service.transition(replay, "completed")
        await session.commit()


async def execute_replay(
    session_factory: async_sessionmaker[AsyncSession],
    replay_id: str,
    pricing: Mapping[str, ModelPrice] | None = None,
    trace_context: Mapping[str, str] | None = None,
) -> str:
    """Returns the replay's final status (or "skipped") -- the arq job result."""
    replay_span = tracing.tracer().start_span(
        "agentforge.replay",
        context=tracing.extract(trace_context),
        attributes={"agentforge.component": "worker", "agentforge.replay.id": replay_id},
    )

    def end_span(error: str | None = None) -> list[dict[str, Any]]:
        if not replay_span.is_recording():
            return []
        if error:
            tracing.error(replay_span, error, event="agentforge.replay.failed")
        replay_span.end()
        return _claim(replay_span)

    try:
        started = await _start(session_factory, replay_id)
        if started is None:
            replay_span.set_attribute("agentforge.replay.skipped", True)
            end_span()
            return "skipped"
        tc: TestCase = started["case"]
        replay_span.set_attributes(
            tracing.attrs(
                {
                    "agentforge.replay.original_result_id": started["original_result_id"],
                    "agentforge.replay.overrides": started["overrides"] or None,
                    "agentforge.case.key": tc.case_key,
                    "agentforge.dataset.content_hash": started["dataset_content_hash"],
                    "agentforge.adapter.type": started["adapter_type"],
                    "agentforge.adapter.target": started["adapter_target"],
                }
            )
        )
        adapter = None
        try:
            pinned = PinnedEvaluators(started["evaluators"])
            adapter = build_adapter(started["adapter_type"], started["adapter_target"])
            overrides = check_overrides(adapter, started["overrides"], started["adapter_target"])
        except OverrideError as exc:
            message = f"overrides rejected: {exc}"
            if adapter is not None:
                await adapter.aclose()
            await _fail(session_factory, replay_id, message, end_span(message))
            return "failed"
        except (AdapterLoadError, ValueError) as exc:  # ValueError: a pinned evaluator version no longer exists
            message = f"replay setup failed: {exc}"
            if adapter is not None:
                await adapter.aclose()
            await _fail(session_factory, replay_id, message, end_span(message))
            return "failed"

        config = EvalConfig(
            threshold=started["threshold"], max_latency_ms=started["max_latency_ms"], pricing=pricing or {}
        )
        token = otel_context.attach(trace.set_span_in_context(replay_span))
        try:
            outcome, scored, case_spans = await _run_case(adapter, tc, started, pinned, config, overrides)
        finally:
            otel_context.detach(token)
            await adapter.aclose()
        await _record(session_factory, replay_id, started, outcome, scored, case_spans)
        await _complete(session_factory, replay_id, end_span())
        log.info("replay %s of case %s -> %s", replay_id, tc.case_key, outcome.status)
        return "completed"
    except asyncio.CancelledError:
        message = "worker stopped or job timed out mid-replay"
        await asyncio.shield(_fail(session_factory, replay_id, message, end_span(message)))
        raise
    except Exception as exc:  # noqa: BLE001 - infrastructure failure (DB down, ...)
        log.exception("replay %s failed", replay_id)
        message = f"worker error: {type(exc).__name__}: {exc}"
        try:
            await _fail(session_factory, replay_id, message, end_span(message))
        except Exception:  # noqa: BLE001
            log.exception("could not mark replay %s failed", replay_id)
        return "failed"
    finally:
        if replay_span.is_recording():
            replay_span.end()
        collector = tracing.collector()
        ctx = replay_span.get_span_context()
        if collector is not None and ctx.is_valid:
            dropped = collector.discard(ctx.trace_id)
            if dropped:
                log.warning("replay %s: %d late span(s) not stored", replay_id, dropped)
