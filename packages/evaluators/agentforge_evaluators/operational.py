"""Measurement evaluators: latency, token usage, estimated cost.

These record a measured `value` + `unit` rather than a 0..1 quality score.
Token counts are whatever the adapter reports -- AgentForge never guesses
them -- and cost is only ever an estimate from configured rates.

Per-case param: latency takes `max_ms` (a per-case budget; default: the
run's max_latency_ms). token_usage and estimated_cost take none.
"""

from __future__ import annotations

from typing import Any

from agentforge_evaluators.base import EvalConfig, EvalInput, MetricOutcome, Params
from agentforge_evaluators.pricing import cost_usd


def _ms(value: float) -> str:
    return f"{value:.2f} ms" if value < 10 else f"{value:.0f} ms"


def latency(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    ms = case.latency_ms
    budget = params.get("max_ms", config.max_latency_ms)
    evidence: dict[str, Any] = {"latency_ms": ms, "measured": "wall-clock time of the adapter call, in the worker"}
    if budget is None:
        return MetricOutcome(
            score=None,
            passed=None,
            reason=f"{_ms(ms)} (no latency budget configured)",
            evidence=evidence,
            value=ms,
            unit="ms",
        )
    passed = ms <= budget
    evidence["max_latency_ms"] = budget
    return MetricOutcome(
        score=None,
        passed=passed,
        reason=f"{_ms(ms)} {'<=' if passed else '>'} budget {_ms(budget)}",
        evidence=evidence,
        value=ms,
        unit="ms",
    )


def token_usage(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    if case.input_tokens is None and case.output_tokens is None:
        return MetricOutcome(
            score=None,
            passed=None,
            reason="not reported by adapter",
            evidence={"source": "adapter-reported", "input_tokens": None, "output_tokens": None},
        )
    total = (case.input_tokens or 0) + (case.output_tokens or 0)
    partial = case.input_tokens is None or case.output_tokens is None
    return MetricOutcome(
        score=None,
        passed=None,
        reason=f"{total} token(s) reported by adapter" + (" (only partially reported)" if partial else ""),
        evidence={
            "source": "adapter-reported",
            "input_tokens": case.input_tokens,
            "output_tokens": case.output_tokens,
            "model": case.model,
            "partial": partial,
        },
        value=float(total),
        unit="tokens",
    )


def estimated_cost(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    labels = ["estimated"]
    if case.input_tokens is None and case.output_tokens is None:
        return MetricOutcome(
            score=None,
            passed=None,
            reason="cannot estimate: adapter reported no token usage",
            evidence={"model": case.model},
            labels=labels,
        )
    if case.model is None:
        return MetricOutcome(
            score=None,
            passed=None,
            reason="cannot estimate: adapter reported no model name",
            evidence={},
            labels=labels,
        )
    price = config.pricing.get(case.model)
    if price is None:
        return MetricOutcome(
            score=None,
            passed=None,
            reason=f"cannot estimate: model '{case.model}' has no entry in the pricing config",
            evidence={"model": case.model, "priced_models": sorted(config.pricing)},
            labels=labels,
        )
    input_tokens = case.input_tokens or 0
    output_tokens = case.output_tokens or 0
    usd = cost_usd(price, input_tokens, output_tokens)
    return MetricOutcome(
        score=None,
        passed=None,
        reason=f"estimated ${usd:.6f} from configured rates for '{case.model}'",
        evidence={
            "formula": "input_tokens/1e6 * input_rate + output_tokens/1e6 * output_rate",
            "model": case.model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_usd_per_million_tokens": price.input_usd_per_million_tokens,
            "output_usd_per_million_tokens": price.output_usd_per_million_tokens,
            "source": "config/pricing.yaml rates x adapter-reported tokens",
        },
        value=usd,
        unit="usd",
        labels=labels,
    )
