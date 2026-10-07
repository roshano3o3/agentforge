"""Execute one evaluation run: pending -> running -> completed | failed.

Idempotent per run id. A finished run is left alone. A run found already
`running` (the previous attempt died mid-way) has its partial results
cleared and starts over, so a result set is always from one complete pass.

Each case's result is committed as soon as it's scored, so GET /runs/{id}
shows live progress. Results are always written *before* the final status
change: once a run is completed, the DB trigger refuses any further result
writes.

Tracing (agentforge_api.tracing): the run is an `agentforge.run` span,
continuing the trace the API started (its context arrives with the job).
Each case is an `agentforge.case` span with the agent call (`invoke_agent`)
and one `evaluate <name>` span per evaluator under it; spans the agent opens
itself land under the agent call. A case's spans are stored with its result,
in the same transaction; the run's span just before its final status.
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
from agentforge_api.models.evaluation import (
    TERMINAL_STATUSES,
    AgentStep,
    EvaluationResult,
    EvaluationRun,
    MetricScore,
    ResultStatus,
    RunStatus,
)
from agentforge_api.models.trace import TraceSpan
from agentforge_api.services import runs as run_service
from agentforge_evaluators import (
    EvalConfig,
    EvalInput,
    EvaluatorSpec,
    MetricOutcome,
    ModelPrice,
    TrajectoryStep,
    compute_aggregates,
    effective_config,
    resolve,
)
from agentforge_sdk.replay import describe_declaration
from agentforge_worker import provenance
from agentforge_worker.adapters import AdapterLoadError, CaseOutcome, HttpAdapter, build_adapter, invoke

log = logging.getLogger("agentforge.worker")


class PinnedEvaluators:
    """Maps a case config key to the evaluator version this run pinned:
    `name@version` must be pinned exactly; a bare `name` uses the run's
    pinned version of it. Keys the run didn't pin (filtered out by an
    explicit --evaluators list) resolve to None and are skipped."""

    def __init__(self, keys: list[str]) -> None:
        self.keys = list(keys)
        self._by_key = {key: resolve(key) for key in keys}
        self._by_name: dict[str, EvaluatorSpec] = {}
        for spec in sorted(self._by_key.values(), key=lambda s: s.version):
            self._by_name[spec.name] = spec

    def lookup(self, key: str) -> EvaluatorSpec | None:
        return self._by_key.get(key) if "@" in key else self._by_name.get(key)


def case_evaluators(
    pinned: PinnedEvaluators, default: Mapping[str, Any] | None, tc: TestCase
) -> list[tuple[EvaluatorSpec, dict[str, Any]]]:
    """The (evaluator, params) pairs that apply to this case -- only those."""
    config = effective_config(default, tc.evaluators, pinned.keys)
    # Phase 2 legacy fields on versions published before per-case config:
    # supply them as the params they always were. The stored rows are untouched.
    for key, params in config.items():
        name = key.partition("@")[0]
        if name == "answer_contains" and "phrases" not in params and tc.expected_answer_contains:
            params["phrases"] = list(tc.expected_answer_contains)
        if name == "answer_regex" and "pattern" not in params and tc.expected_answer_regex:
            params["pattern"] = tc.expected_answer_regex
    applied = []
    for key, params in config.items():
        spec = pinned.lookup(key)
        if spec is not None:
            applied.append((spec, params))
    return applied


def score_case(
    evaluators: list[tuple[EvaluatorSpec, dict[str, Any]]], case: EvalInput, config: EvalConfig
) -> list[tuple[EvaluatorSpec, MetricOutcome]]:
    """Run each configured evaluator with its params, each in its own span;
    an evaluator that raises is recorded as a failed metric for this case
    (and an ERROR span), never propagated."""
    scored = []
    for spec, params in evaluators:
        with tracing.span(
            f"evaluate {spec.name}",
            {
                "agentforge.component": "worker",
                "agentforge.evaluator.name": spec.name,
                "agentforge.evaluator.version": spec.version,
                "agentforge.evaluator.kind": spec.kind,
                "agentforge.evaluator.params": params or None,
            },
        ) as s:
            try:
                outcome = spec.fn(case, config, params)
            except Exception as exc:  # noqa: BLE001
                tracing.error(s, f"{type(exc).__name__}: {exc}", exc)
                outcome = MetricOutcome(
                    score=None,
                    passed=False,
                    reason=f"evaluator error: {type(exc).__name__}: {exc}",
                    evidence={"error_type": type(exc).__name__},
                    labels=["evaluator-error"],
                )
            if params:
                outcome.evidence = {**outcome.evidence, "params": params}
            s.set_attributes(
                tracing.attrs(
                    {
                        "agentforge.evaluator.passed": outcome.passed,
                        "agentforge.evaluator.score": outcome.score,
                        "agentforge.evaluator.value": outcome.value,
                        "agentforge.evaluator.reason": outcome.reason,
                        "agentforge.evaluator.failing_steps": outcome.evidence.get("failing_steps"),
                    }
                )
            )
            if outcome.passed is False:
                s.add_event("agentforge.evaluator.failed", tracing.attrs({"reason": outcome.reason}))
        scored.append((spec, outcome))
    return scored


def _eval_input(tc: TestCase, outcome: CaseOutcome) -> EvalInput:
    out = outcome.output
    assert out is not None
    return EvalInput(
        input=tc.input,
        expected_answer=tc.expected_answer,
        expected_context=list(tc.expected_context or []),
        answer=out.answer,
        retrieved_doc_ids=out.retrieved_doc_ids,
        citations=out.citations,
        latency_ms=outcome.latency_ms,
        input_tokens=out.input_tokens,
        output_tokens=out.output_tokens,
        model=out.model,
        steps=tuple(
            TrajectoryStep(index=i, kind=s.kind, name=s.name, args=dict(s.args), result=s.result, error=s.error)
            for i, s in enumerate(out.steps, start=1)
        ),
        trajectory=tc.trajectory,
        safety=tc.safety,
    )


async def _store_run_spans(session: AsyncSession, run_id: str, spans: list[dict[str, Any]]) -> None:
    """Run-level spans, flushed before the final status change (after it, the trigger refuses them)."""
    if spans:
        await session.execute(insert(TraceSpan), [{**v, "run_id": run_id, "evaluation_result_id": None} for v in spans])


async def _fail(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    message: str,
    spans: list[dict[str, Any]] | None = None,
) -> None:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        if run is not None and run.status not in TERMINAL_STATUSES:
            await _store_run_spans(session, run_id, spans or [])
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
        # What this run executes with (also on a restart: it's the code running now).
        for key, value in provenance.collect(run.adapter_type, run.adapter_target).items():
            setattr(run, key, value)
        if run.status == RunStatus.pending:
            run_service.transition(run, RunStatus.running)
        else:
            log.warning("run %s was already running (previous attempt died); restarting it from scratch", run_id)
            result_ids = select(EvaluationResult.id).where(EvaluationResult.evaluation_run_id == run_id)
            await session.execute(delete(AgentStep).where(AgentStep.evaluation_result_id.in_(result_ids)))
            await session.execute(delete(MetricScore).where(MetricScore.evaluation_result_id.in_(result_ids)))
            await session.execute(delete(EvaluationResult).where(EvaluationResult.evaluation_run_id == run_id))
        await session.commit()

        cases = list(
            await session.scalars(
                select(TestCase)
                .where(TestCase.dataset_version_id == run.dataset_version_id)
                .order_by(TestCase.case_key)
            )
        )
        version = await session.get(DatasetVersion, run.dataset_version_id)
        assert version is not None
        return {
            "cases": cases,
            "dataset_content_hash": content_hash_of(version.default_evaluators, cases),
            "dataset_version_id": version.id,
            "default_evaluators": version.default_evaluators,
            "evaluators": list(run.evaluators),
            "adapter_type": run.adapter_type,
            "adapter_target": run.adapter_target,
            "timeout": run.timeout_seconds or 10.0,
            "threshold": run.threshold,
            "max_latency_ms": run.max_latency_ms,
            "labels": run_service.run_labels(run),
        }


def replay_options(adapter: Any) -> dict[str, Any]:
    """What a replay of this run's cases may override: the adapter's own declaration."""
    reason = (
        "HTTP adapters can't declare replay settings (agentforge_sdk.replay); replay without overrides"
        if isinstance(adapter, HttpAdapter)
        else "the adapter declares no replay settings (agentforge_sdk.replay.replayable); replay without overrides"
    )
    return describe_declaration(adapter.replay_settings, reason_if_none=reason)


async def _record_replay_options(session_factory: async_sessionmaker[AsyncSession], run_id: str, adapter: Any) -> None:
    async with session_factory() as session:
        run = await session.get(EvaluationRun, run_id)
        assert run is not None
        run.replay_options = replay_options(adapter)
        await session.commit()


async def _record(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    tc: TestCase,
    outcome: CaseOutcome,
    scored: list[tuple[EvaluatorSpec, MetricOutcome]],
    run_labels: list[str],
    spans: list[dict[str, Any]] | None = None,
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
        for i, step in enumerate(out.steps if out else [], start=1):
            session.add(
                AgentStep(
                    evaluation_result_id=result.id,
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
        if spans:
            # One multi-row INSERT for the case's spans (after the result, scores and steps are flushed).
            await session.flush()
            await session.execute(
                insert(TraceSpan), [{**v, "run_id": run_id, "evaluation_result_id": result.id} for v in spans]
            )
        await session.commit()


async def _complete(
    session_factory: async_sessionmaker[AsyncSession], run_id: str, spans: list[dict[str, Any]] | None = None
) -> None:
    async with session_factory() as session:
        run = await run_service.load_run(session, run_id)
        assert run is not None
        await _store_run_spans(session, run_id, spans or [])
        aggregates = compute_aggregates(run_service.case_records(run.results))
        run.aggregates = aggregates
        run_service.transition(run, RunStatus.completed)
        await session.commit()


def _claim(span: trace.Span) -> list[dict[str, Any]]:
    """The ended `span` and its buffered descendants, as trace_spans row values."""
    collector = tracing.collector()
    ctx = span.get_span_context()
    if collector is None or not ctx.is_valid:
        return []
    return [tracing.row_values(s) for s in collector.take_subtree(ctx.trace_id, ctx.span_id)]


def _case_attributes(tc: TestCase, started: dict) -> dict[str, Any]:
    safety = tc.safety or {}
    return {
        "agentforge.component": "worker",
        "agentforge.case.id": tc.id,
        "agentforge.case.key": tc.case_key,
        "agentforge.case.input": tc.input,
        "agentforge.case.tags": list(tc.tags or []) or None,
        "agentforge.dataset.version_id": started["dataset_version_id"],
        "agentforge.dataset.content_hash": started["dataset_content_hash"],
        "agentforge.attack.category": safety.get("category"),
        "agentforge.attack.technique": safety.get("technique"),
        "agentforge.attack.source_case": safety.get("source_case"),
    }


async def _run_case(
    adapter: Any,
    tc: TestCase,
    started: dict,
    pinned: PinnedEvaluators,
    config: EvalConfig,
    overrides: dict[str, Any] | None = None,
) -> tuple[CaseOutcome, list[tuple[EvaluatorSpec, MetricOutcome]], list[dict[str, Any]]]:
    """One case under its own span: the agent call, then each evaluator.
    `overrides`: a replay's validated overrides (agentforge_worker.replay)."""
    with tracing.span("agentforge.case", _case_attributes(tc, started)) as case_span:
        with tracing.span(
            "invoke_agent",
            {
                "agentforge.component": "worker",
                "gen_ai.operation.name": "invoke_agent",
                "agentforge.adapter.type": started["adapter_type"],
                "agentforge.adapter.target": started["adapter_target"],
                "agentforge.agent.input": tc.input,
                "agentforge.agent.scenario": tc.scenario,
            },
        ) as call_span:
            outcome = await invoke(adapter, tc.input, tc.case_key, started["timeout"], tc.scenario, overrides)
            out = outcome.output
            if out is not None:
                call_span.set_attributes(
                    tracing.attrs(
                        {
                            "agentforge.agent.output": out.answer,
                            "agentforge.agent.retrieved_doc_ids": out.retrieved_doc_ids or None,
                            "agentforge.agent.citations": out.citations or None,
                            "agentforge.agent.steps": len(out.steps),
                            # GenAI conventions, only for what the adapter reported.
                            "gen_ai.request.model": out.model,
                            "gen_ai.usage.input_tokens": out.input_tokens,
                            "gen_ai.usage.output_tokens": out.output_tokens,
                        }
                    )
                )
            else:
                first_line = (outcome.error_message or "").splitlines()[0] if outcome.error_message else ""
                tracing.error(
                    call_span,
                    f"{outcome.status}: {outcome.error_type}: {first_line}",
                    event="agentforge.adapter.error" if outcome.status == "error" else "agentforge.adapter.timeout",
                )
            call_span.set_attribute("agentforge.agent.latency_ms", outcome.latency_ms)
        scored = (
            score_case(case_evaluators(pinned, started["default_evaluators"], tc), _eval_input(tc, outcome), config)
            if outcome.status == "ok"
            else []
        )
        passed = outcome.status == "ok" and not any(m.passed is False for _spec, m in scored)
        case_span.set_attributes(
            {
                "agentforge.case.status": outcome.status,
                "agentforge.case.passed": passed,
                "agentforge.case.failed_evaluators": ",".join(sorted(s.name for s, m in scored if m.passed is False)),
            }
        )
        if outcome.status != "ok":
            tracing.error(case_span, f"case {outcome.status}: {outcome.error_type}", event="agentforge.case.error")
    return outcome, scored, _claim(case_span)


async def execute_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: str,
    pricing: Mapping[str, ModelPrice] | None = None,
    trace_context: Mapping[str, str] | None = None,
) -> str:
    """Returns the run's final status (or a no-op reason) -- used as the arq job result.

    `trace_context` is the W3C carrier the API put on the job; the run's span
    continues that trace (or starts one if there's none)."""
    run_span = tracing.tracer().start_span(
        "agentforge.run",
        context=tracing.extract(trace_context),
        attributes={"agentforge.component": "worker", "agentforge.run.id": run_id},
    )

    def end_run_span(error: str | None = None) -> list[dict[str, Any]]:
        if not run_span.is_recording():
            return []
        if error:
            tracing.error(run_span, error, event="agentforge.run.failed")
        run_span.end()
        return _claim(run_span)

    try:
        started = await _start(session_factory, run_id)
        if started is None:
            run_span.set_attribute("agentforge.run.skipped", True)
            end_run_span()
            return "skipped"
        run_span.set_attributes(
            tracing.attrs(
                {
                    "agentforge.dataset.version_id": started["dataset_version_id"],
                    "agentforge.dataset.content_hash": started["dataset_content_hash"],
                    "agentforge.adapter.type": started["adapter_type"],
                    "agentforge.adapter.target": started["adapter_target"],
                    "agentforge.run.case_count": len(started["cases"]),
                }
            )
        )
        try:
            pinned = PinnedEvaluators(started["evaluators"])
            adapter = build_adapter(started["adapter_type"], started["adapter_target"])
        except (AdapterLoadError, ValueError) as exc:
            message = f"run setup failed: {exc}"
            await _fail(session_factory, run_id, message, end_run_span(message))
            return "failed"

        await _record_replay_options(session_factory, run_id, adapter)
        config = EvalConfig(
            threshold=started["threshold"], max_latency_ms=started["max_latency_ms"], pricing=pricing or {}
        )
        token = otel_context.attach(trace.set_span_in_context(run_span))
        try:
            for tc in started["cases"]:
                outcome, scored, spans = await _run_case(adapter, tc, started, pinned, config)
                await _record(session_factory, run_id, tc, outcome, scored, started["labels"], spans)
                log.info("run %s case %s -> %s (%.0f ms)", run_id, tc.case_key, outcome.status, outcome.latency_ms)
        finally:
            otel_context.detach(token)
            await adapter.aclose()

        await _complete(session_factory, run_id, end_run_span())
        log.info("run %s completed (%d case(s))", run_id, len(started["cases"]))
        return "completed"
    except asyncio.CancelledError:
        # arq job timeout or worker shutdown mid-run. Mark it failed (terminal,
        # visible) rather than leaving it `running` with half its results.
        message = "worker stopped or job timed out mid-run"
        await asyncio.shield(_fail(session_factory, run_id, message, end_run_span(message)))
        raise
    except Exception as exc:  # noqa: BLE001 - infrastructure failure (DB down, ...)
        log.exception("run %s failed", run_id)
        message = f"worker error: {type(exc).__name__}: {exc}"
        try:
            await _fail(session_factory, run_id, message, end_run_span(message))
        except Exception:  # noqa: BLE001
            log.exception("could not mark run %s failed", run_id)
        return "failed"
    finally:
        if run_span.is_recording():
            run_span.end()
        collector = tracing.collector()
        ctx = run_span.get_span_context()
        if collector is not None and ctx.is_valid:
            # Anything still buffered for this trace (e.g. a hung adapter
            # thread's spans) can no longer be stored with its case.
            dropped = collector.discard(ctx.trace_id)
            if dropped:
                log.warning("run %s: %d late span(s) not stored", run_id, dropped)
