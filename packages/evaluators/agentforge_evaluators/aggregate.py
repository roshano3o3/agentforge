"""Run-level aggregates, computed once from a finished run's persisted rows.

Definitions (so every number on the dashboard is derivable by hand):

* pass_rate = cases that passed / all cases. A case passes when its adapter
  call succeeded (status "ok") and no evaluator returned passed=False.
  Errored and timed-out cases count as not passed.
* latency p50/p95: nearest-rank percentile over the latencies of *ok* cases
  only (a timeout's latency is just the configured timeout, and an error's
  is how long it took to crash -- neither is the app's real latency).
* per-metric mean_score: mean of non-null 0..1 scores; mean_value: mean of
  non-null measured values (ms, tokens, usd). "not applicable" results are
  counted separately and excluded from means.
* estimated_cost_usd.total: sum over cases where a cost could be estimated;
  cases where it couldn't are counted, not treated as $0.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricRecord:
    evaluator_name: str
    evaluator_version: str
    score: float | None
    value: float | None
    unit: str | None
    passed: bool | None
    reason: str = ""


@dataclass(frozen=True)
class CaseRecord:
    status: str  # "ok" | "error" | "timeout"
    passed: bool
    latency_ms: float
    metrics: list[MetricRecord] = field(default_factory=list)


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least pct% of
    values <= it. No interpolation, so the result is always an observed value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def compute_aggregates(cases: Iterable[CaseRecord]) -> dict:
    cases = list(cases)
    ok_latencies = [c.latency_ms for c in cases if c.status == "ok"]

    metrics: dict[str, dict] = {}
    for case in cases:
        for m in case.metrics:
            key = f"{m.evaluator_name}@{m.evaluator_version}"
            entry = metrics.setdefault(
                key,
                {
                    "name": m.evaluator_name,
                    "version": m.evaluator_version,
                    "unit": m.unit,
                    "_scores": [],
                    "_values": [],
                    "_verdicts": [],
                    "not_applicable_count": 0,
                },
            )
            if m.unit and not entry["unit"]:
                entry["unit"] = m.unit
            if m.score is not None:
                entry["_scores"].append(m.score)
            if m.value is not None:
                entry["_values"].append(m.value)
            if m.passed is not None:
                entry["_verdicts"].append(m.passed)
            if m.score is None and m.value is None and m.passed is None:
                entry["not_applicable_count"] += 1

    metrics_out = {}
    for key, e in sorted(metrics.items()):
        verdicts = e.pop("_verdicts")
        scores = e.pop("_scores")
        values = e.pop("_values")
        metrics_out[key] = {
            **e,
            "mean_score": _mean(scores),
            "mean_value": _mean(values),
            "scored_count": len(scores),
            "valued_count": len(values),
            "pass_rate": (sum(verdicts) / len(verdicts)) if verdicts else None,
        }

    cost_key = next((k for k in metrics_out if k.startswith("estimated_cost@")), None)
    costs = [
        m.value
        for c in cases
        for m in c.metrics
        if m.evaluator_name == "estimated_cost" and m.value is not None
    ]
    cost_block = None
    if cost_key is not None:
        cost_block = {
            "label": "estimated",
            "total": sum(costs) if costs else None,
            "cases_with_estimate": len(costs),
            "cases_without_estimate": len(cases) - len(costs),
        }

    passed_count = sum(1 for c in cases if c.passed)
    return {
        "case_count": len(cases),
        "ok_count": sum(1 for c in cases if c.status == "ok"),
        "error_count": sum(1 for c in cases if c.status == "error"),
        "timeout_count": sum(1 for c in cases if c.status == "timeout"),
        "passed_count": passed_count,
        "pass_rate": (passed_count / len(cases)) if cases else None,
        "latency_ms": {
            "basis": "ok cases only, nearest-rank",
            "p50": percentile(ok_latencies, 50),
            "p95": percentile(ok_latencies, 95),
            "mean": _mean(ok_latencies),
        },
        "estimated_cost_usd": cost_block,
        "metrics": metrics_out,
    }
