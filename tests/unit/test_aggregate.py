from __future__ import annotations

import pytest

from agentforge_evaluators import CaseRecord, MetricRecord, compute_aggregates, percentile


def test_percentile_is_nearest_rank_with_no_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 100.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None


def _metric(name: str, **kw) -> MetricRecord:
    defaults = {"score": None, "value": None, "unit": None, "passed": None}
    return MetricRecord(evaluator_name=name, evaluator_version="1.0.0", **{**defaults, **kw})


def test_compute_aggregates() -> None:
    cases = [
        CaseRecord("ok", True, 10.0, [_metric("m", score=1.0, passed=True), _metric("estimated_cost", value=0.5, unit="usd")]),
        CaseRecord("ok", False, 30.0, [_metric("m", score=0.5, passed=False), _metric("estimated_cost")]),
        CaseRecord("timeout", False, 5000.0, []),
        CaseRecord("error", False, 1.0, []),
    ]
    agg = compute_aggregates(cases)

    assert (agg["case_count"], agg["ok_count"], agg["error_count"], agg["timeout_count"]) == (4, 2, 1, 1)
    assert agg["passed_count"] == 1
    assert agg["pass_rate"] == pytest.approx(0.25)  # errored/timed-out cases count as not passed
    # Latency percentiles over ok cases only: the 5000ms timeout is excluded.
    assert agg["latency_ms"]["p50"] == 10.0
    assert agg["latency_ms"]["p95"] == 30.0
    m = agg["metrics"]["m@1.0.0"]
    assert m["mean_score"] == pytest.approx(0.75)
    assert m["pass_rate"] == pytest.approx(0.5)
    cost = agg["estimated_cost_usd"]
    assert cost == {"label": "estimated", "total": 0.5, "cases_with_estimate": 1, "cases_without_estimate": 3}
    assert agg["metrics"]["estimated_cost@1.0.0"]["not_applicable_count"] == 1


def test_empty_run() -> None:
    agg = compute_aggregates([])
    assert agg["pass_rate"] is None
    assert agg["latency_ms"]["p50"] is None
    assert agg["estimated_cost_usd"] is None
