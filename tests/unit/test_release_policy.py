"""Release policy parsing (validated on load) and every gate check type,
over hand-built run snapshots -- the gate is pure arithmetic, so each
expected number here is worked out by hand."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentforge_cli.release_io import ConfigError, load_config, markdown_report, verdict_line
from agentforge_evaluators import (
    CaseOutcome,
    PolicyError,
    RegressionError,
    RunSnapshot,
    compare,
    evaluate_policy,
    parse_policy,
)


def snap(
    run_id: str = "r",
    *,
    pass_rate: float | None = 1.0,
    p95: float | None = 100.0,
    p50: float | None = 50.0,
    cost: float | None = None,
    errors: int = 0,
    cases: dict[str, CaseOutcome] | None = None,
    evaluators: dict[str, dict[str, Any]] | None = None,
    dataset: str = "dv1",
) -> RunSnapshot:
    metrics = {
        f"{name}@{e.get('version', '1.0.0')}": {"name": name, "version": e.get("version", "1.0.0"), **e}
        for name, e in (evaluators or {}).items()
    }
    return RunSnapshot(
        run_id=run_id,
        dataset_version_id=dataset,
        aggregates={
            "case_count": 10,
            "error_count": errors,
            "timeout_count": 0,
            "pass_rate": pass_rate,
            "latency_ms": {"p50": p50, "p95": p95},
            "estimated_cost_usd": None if cost is None else {"total": cost},
            "metrics": metrics,
        },
        cases=cases or {},
    )


def ok(tags: tuple[str, ...] = ()) -> CaseOutcome:
    return CaseOutcome(status="ok", passed=True, tags=tags)


def bad(tags: tuple[str, ...] = (), *by: str) -> CaseOutcome:
    return CaseOutcome(status="ok", passed=False, tags=tags, failed_evaluators=by)


def checks(policy: dict[str, Any], baseline: RunSnapshot, candidate: RunSnapshot) -> list[dict[str, Any]]:
    return evaluate_policy(parse_policy(policy), baseline, candidate)


def only(policy: dict[str, Any], baseline: RunSnapshot, candidate: RunSnapshot) -> dict[str, Any]:
    [check] = checks(policy, baseline, candidate)
    return check


# -- parsing ----------------------------------------------------------------------


FULL = {
    "minimums": {"pass_rate": 0.9, "tool_selection.mean_score": 0.8, "approval_required.pass_rate": 1},
    "maximums": {"p95_latency_ms": 500, "error_rate": 0, "estimated_cost_usd": 0.05, "token_usage.mean_value": 900},
    "regressions": {
        "pass_rate": {"max_drop": 0.02},
        "tool_selection.mean_score": {"max_drop_pct": 5},
        "p95_latency_ms": {"max_increase": 100, "max_increase_pct": 25},
    },
    "cases": {"max_newly_failing": 0, "no_newly_failing_tags": ["critical"]},
}


def test_full_policy_parses_and_round_trips() -> None:
    policy = parse_policy(FULL)
    assert policy.minimums["approval_required.pass_rate"] == 1.0
    assert policy.regressions["p95_latency_ms"] == {"max_increase": 100.0, "max_increase_pct": 25.0}
    assert policy.no_newly_failing_tags == ("critical",)
    assert parse_policy(policy.to_dict()) == policy


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"minimum": {"pass_rate": 0.9}}, r"unknown key\(s\) \['minimum'\]"),
        ({"minimums": {"passrate": 0.9}}, "unknown metric 'passrate'"),
        ({"minimums": {"made_up.mean_score": 0.9}}, "unknown evaluator 'made_up'"),
        ({"minimums": {"tool_selection.median": 0.9}}, "unknown metric"),
        ({"minimums": {"p95_latency_ms": 100}}, "lower-is-better, so a minimum doesn't make sense"),
        ({"maximums": {"pass_rate": 0.5}}, "higher-is-better, so a maximum doesn't make sense"),
        ({"minimums": {"pass_rate": 90}}, "between 0 and 1"),
        ({"minimums": {"pass_rate": True}}, "must be a number"),
        ({"minimums": {"pass_rate": "0.9"}}, "must be a number"),
        ({"regressions": {"p95_latency_ms": {"max_drop": 10}}}, "lower-is-better; use max_increase"),
        ({"regressions": {"pass_rate": {"max_increase": 0.1}}}, "higher-is-better; use max_drop"),
        ({"regressions": {"pass_rate": {}}}, "needs one of"),
        ({"regressions": {"pass_rate": {"max_drop": -0.1}}}, "must be >= 0"),
        ({"regressions": {"pass_rate": 0.02}}, "must be a mapping"),
        ({"cases": {"max_newly_failing": -1}}, "integer >= 0"),
        ({"cases": {"no_newly_failing_tags": "critical"}}, "list of tag names"),
        ({"cases": {"newly_failing": 0}}, "unknown key"),
        ({}, "defines no checks"),
        ([], "must be a mapping"),
    ],
)
def test_invalid_policies_are_rejected_on_load(raw: Any, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        parse_policy(raw)


def test_config_file_is_validated_on_load(tmp_path: Path) -> None:
    good = tmp_path / "agentforge.yaml"
    good.write_text("api_url: http://127.0.0.1:9\nrelease_policy:\n  minimums: {pass_rate: 0.9}\n", "utf-8")
    config = load_config(good)
    assert config.api_url == "http://127.0.0.1:9"
    assert config.policy is not None and config.policy.minimums == {"pass_rate": 0.9}

    typo = tmp_path / "typo.yaml"
    typo.write_text("release_policy:\n  maximums: {pass_rate: 0.5}\n", "utf-8")
    with pytest.raises(ConfigError, match=r"typo.yaml: release_policy: maximums.pass_rate: pass_rate is higher"):
        load_config(typo)

    stray = tmp_path / "stray.yaml"
    stray.write_text("release:\n  minimum: {}\n", "utf-8")
    with pytest.raises(ConfigError, match=r"unknown key\(s\) \['release'\]"):
        load_config(stray)

    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")

    # The repo's own example policy is valid.
    repo_config = load_config(Path(__file__).resolve().parents[2] / "agentforge.yaml")
    assert repo_config.policy is not None and repo_config.policy.no_newly_failing_tags == ("critical",)


# -- comparison --------------------------------------------------------------------


def test_compare_refuses_runs_of_different_dataset_versions() -> None:
    with pytest.raises(RegressionError, match="different dataset versions"):
        compare(snap(dataset="dv1"), snap(dataset="dv2"))


def test_compare_classifies_every_case_and_computes_deltas() -> None:
    base = snap(
        "b",
        pass_rate=0.5,
        p95=200.0,
        cases={"a": ok(), "b": ok(), "c": bad(), "d": bad()},
        evaluators={"tool_selection": {"mean_score": 0.8, "pass_rate": 0.5}},
    )
    cand = snap(
        "c",
        pass_rate=0.5,
        p95=150.0,
        cases={"a": ok(), "b": bad(("critical",), "approval_required"), "c": ok(), "d": bad()},
        evaluators={"tool_selection": {"mean_score": 0.6, "pass_rate": 0.5}},
    )
    report = compare(base, cand)
    assert report["case_counts"] == {"newly_failing": 1, "fixed": 1, "still_failing": 1, "still_passing": 1}
    [newly] = report["cases"]["newly_failing"]
    assert newly == {
        "case_key": "b",
        "tags": ["critical"],
        "baseline_status": "ok",
        "candidate_status": "ok",
        "candidate_failed_evaluators": ["approval_required"],
    }
    assert report["summary"]["p95_latency_ms"] == {
        "baseline": 200.0,
        "candidate": 150.0,
        "delta": -50.0,
        "delta_pct": -25.0,
    }
    [ts] = report["metrics"]
    assert ts["comparable"] is True
    assert ts["mean_score"]["delta"] == pytest.approx(-0.2)
    assert ts["mean_score"]["delta_pct"] == pytest.approx(-25.0)
    # No cost estimate in either run: deltas are None, never 0.
    assert report["summary"]["estimated_cost_usd"]["delta"] is None


def test_percentage_delta_is_undefined_when_the_baseline_is_zero() -> None:
    report = compare(snap(errors=0), snap(errors=2))
    assert report["summary"]["error_rate"] == {"baseline": 0.0, "candidate": 0.2, "delta": 0.2, "delta_pct": None}


# -- check types -------------------------------------------------------------------


def test_minimum_check() -> None:
    passing = only({"minimums": {"pass_rate": 0.9}}, snap(pass_rate=1.0), snap(pass_rate=0.9))
    assert passing["passed"] is True and passing["reason"] == "0.9 >= 0.9"
    assert passing["kind"] == "minimum" and passing["threshold"] == 0.9
    assert passing["baseline"] == 1.0 and passing["candidate"] == 0.9 and passing["delta"] == pytest.approx(-0.1)
    failing = only({"minimums": {"pass_rate": 0.9}}, snap(pass_rate=1.0), snap(pass_rate=0.4))
    assert failing["passed"] is False and failing["reason"] == "0.4 < 0.9"


def test_maximum_check() -> None:
    assert only({"maximums": {"p95_latency_ms": 100}}, snap(), snap(p95=100.0))["passed"] is True
    failing = only({"maximums": {"p95_latency_ms": 100}}, snap(), snap(p95=101.5))
    assert failing["passed"] is False and failing["reason"] == "101.5 > 100"
    assert only({"maximums": {"error_rate": 0}}, snap(), snap(errors=1))["passed"] is False


def test_unmeasured_value_fails_its_check() -> None:
    # No cost estimate: a maximum on cost can't be verified, so it fails.
    check = only({"maximums": {"estimated_cost_usd": 1.0}}, snap(), snap(cost=None))
    assert check["passed"] is False
    assert check["reason"] == "estimated_cost_usd not measured in the candidate"
    # An evaluator the candidate never ran.
    check = only({"minimums": {"tool_selection.mean_score": 0.5}}, snap(), snap())
    assert check["passed"] is False and "not measured" in check["reason"]


def test_regression_max_drop_is_direction_aware_and_float_safe() -> None:
    # 1.0 -> 0.98 is a drop of exactly 2 points (0.020000000000000018 in floats): allowed.
    ok_check = only({"regressions": {"pass_rate": {"max_drop": 0.02}}}, snap(pass_rate=1.0), snap(pass_rate=0.98))
    assert ok_check["passed"] is True
    assert ok_check["rule"] == "drop <= 0.02"
    fail = only({"regressions": {"pass_rate": {"max_drop": 0.02}}}, snap(pass_rate=1.0), snap(pass_rate=0.97))
    assert fail["passed"] is False
    assert fail["reason"] == "drop 0.03 (1 -> 0.97) exceeds 0.02"
    # An improvement always passes.
    better = only({"regressions": {"pass_rate": {"max_drop": 0}}}, snap(pass_rate=0.5), snap(pass_rate=0.8))
    assert better["passed"] is True and better["reason"].startswith("improved")


def test_regression_percentage_rules() -> None:
    policy = {"regressions": {"p95_latency_ms": {"max_increase_pct": 25}}}
    assert only(policy, snap(p95=100.0), snap(p95=125.0))["passed"] is True
    fail = only(policy, snap(p95=100.0), snap(p95=130.0))
    assert fail["passed"] is False and fail["reason"] == "increase 30% (100 -> 130) exceeds 25%"
    assert fail["delta_pct"] == pytest.approx(30.0)

    drop = {"regressions": {"tool_selection.mean_score": {"max_drop_pct": 10}}}
    ev = {"tool_selection": {"mean_score": 0.8}}
    lower = {"tool_selection": {"mean_score": 0.7}}
    assert only(drop, snap(evaluators=ev), snap(evaluators=lower))["passed"] is False  # 12.5% drop

    # Baseline 0: any increase is an undefined (infinite) percentage -> fails.
    zero = {"regressions": {"error_rate": {"max_increase_pct": 50}}}
    assert only(zero, snap(errors=0), snap(errors=0))["passed"] is True
    assert only(zero, snap(errors=0), snap(errors=1))["passed"] is False


def test_regression_absolute_increase() -> None:
    policy = {"regressions": {"p95_latency_ms": {"max_increase": 50}}}
    assert only(policy, snap(p95=100.0), snap(p95=150.0))["passed"] is True
    assert only(policy, snap(p95=100.0), snap(p95=150.1))["passed"] is False


def test_regression_needs_both_values_and_the_same_evaluator_version() -> None:
    policy = {"regressions": {"tool_selection.mean_score": {"max_drop": 0.1}}}
    missing = only(policy, snap(), snap(evaluators={"tool_selection": {"mean_score": 1.0}}))
    assert missing["passed"] is False and missing["reason"] == "tool_selection.mean_score not measured in the baseline"
    v1 = {"tool_selection": {"mean_score": 1.0, "version": "1.0.0"}}
    v2 = {"tool_selection": {"mean_score": 1.0, "version": "2.0.0"}}
    mismatch = only(policy, snap(evaluators=v1), snap(evaluators=v2))
    assert mismatch["passed"] is False
    assert mismatch["reason"] == "evaluator versions differ (1.0.0 vs 2.0.0): not comparable"


def test_case_checks() -> None:
    base = snap(cases={"x": ok(("critical",)), "y": ok(), "z": bad(("critical",))})
    cand = snap(cases={"x": ok(("critical",)), "y": bad((), "tool_args"), "z": bad(("critical",))})

    newly = only({"cases": {"max_newly_failing": 0}}, base, cand)
    assert newly["passed"] is False and newly["reason"] == "1 newly failing (y)"
    assert newly["candidate"] == 1.0 and newly["threshold"] == 0.0
    assert only({"cases": {"max_newly_failing": 1}}, base, cand)["passed"] is True

    # y isn't critical, and z was already failing: no newly failing critical case.
    tag = only({"cases": {"no_newly_failing_tags": ["critical"]}}, base, cand)
    assert tag["passed"] is True and tag["metric"] == "newly_failing[tag=critical]"
    cand2 = snap(cases={"x": bad(("critical",), "approval_required"), "y": ok(), "z": bad(("critical",))})
    tag2 = only({"cases": {"no_newly_failing_tags": ["critical"]}}, base, cand2)
    assert tag2["passed"] is False and tag2["reason"] == "newly failing 'critical' case(s): x"


def test_every_check_in_policy_order_and_markdown_report() -> None:
    base = snap(pass_rate=1.0, cases={"x": ok(("critical",))})
    cand = snap(pass_rate=0.4, cases={"x": bad(("critical",), "approval_required")})
    result = checks(FULL | {"maximums": {"p95_latency_ms": 500}}, base, cand)
    assert [c["kind"] for c in result][:3] == ["minimum", "minimum", "minimum"]
    assert {c["kind"] for c in result} == {"minimum", "maximum", "regression", "cases"}

    decision = {
        "id": "d1",
        "application_name": "app",
        "candidate_run_id": "c",
        "baseline_run_id": "b",
        "baseline_ref": "production",
        "passed": all(c["passed"] for c in result),
        "checks": result,
        "regression": compare(base, cand),
    }
    md = markdown_report(decision)
    assert md.startswith("## AgentForge release gate: FAILED")
    assert "| **FAIL** | minimum | `pass_rate` | 1 | 0.4 | -0.6 (-60.0%) | >= 0.9 | 0.4 < 0.9 |" in md
    assert "- `x` [critical]: approval_required" in md
    assert md.rstrip().endswith("**RELEASE GATE: FAILED**")
    assert verdict_line(True) == "RELEASE GATE: PASSED"
