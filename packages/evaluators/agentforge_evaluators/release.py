"""Regression comparison and release-gate checks: pure arithmetic over two
finished runs' stored aggregates and per-case verdicts. No model, no I/O.

Metrics a policy can name (direction decides which rules make sense):

    pass_rate                 higher is better   fraction of cases passed (0..1)
    error_rate                lower is better    (error + timeout cases) / cases
    p50_latency_ms            lower is better    nearest-rank, ok cases only
    p95_latency_ms            lower is better
    estimated_cost_usd        lower is better    total ESTIMATED cost (None if not estimated)
    <evaluator>.mean_score    higher is better   e.g. tool_selection.mean_score
    <evaluator>.pass_rate     higher is better   over the cases the evaluator applied to
    <evaluator>.mean_value    lower is better    measurements: ms, tokens, usd, steps
    safety.<category>.pass_rate higher is better case pass rate of one attack category
                                                 (aggregates.safety.by_category), e.g.
                                                 safety.unauthorized_tool.pass_rate
    safety.injection.pass_rate  higher is better pooled over injection_direct and
                                                 injection_indirect (passed / cases of both)

Policy (the `release_policy` block of agentforge.yaml):

    release_policy:
      minimums:    {pass_rate: 0.9}                       # candidate >= value
      maximums:    {p95_latency_ms: 500}                  # candidate <= value
      regressions:                                        # candidate vs baseline
        pass_rate: {max_drop: 0.02}                       # higher-better: max_drop, max_drop_pct
        p95_latency_ms: {max_increase_pct: 25}            # lower-better: max_increase, max_increase_pct
      cases:
        max_newly_failing: 0
        no_newly_failing_tags: [critical]

`max_drop: 0.02` on pass_rate is 2 percentage points (pass_rate is a
fraction). Comparisons allow a 1e-9 slack so 1.0 - 0.98 isn't read as
0.020000000000000018 > 0.02.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from agentforge_evaluators.registry import UnknownEvaluatorError, resolve
from agentforge_evaluators.safety import ATTACK_CATEGORIES

Direction = Literal["higher", "lower"]
EPSILON = 1e-9

RUN_METRICS: dict[str, Direction] = {
    "pass_rate": "higher",
    "error_rate": "lower",
    "p50_latency_ms": "lower",
    "p95_latency_ms": "lower",
    "estimated_cost_usd": "lower",
}
EVALUATOR_STATS: dict[str, Direction] = {"mean_score": "higher", "pass_rate": "higher", "mean_value": "lower"}
FRACTION_METRICS = {"pass_rate", "error_rate"}
REGRESSION_KEYS: dict[Direction, tuple[str, str]] = {
    "higher": ("max_drop", "max_drop_pct"),
    "lower": ("max_increase", "max_increase_pct"),
}
CASE_CLASSES = ("newly_failing", "fixed", "still_failing", "still_passing")
# Pooled safety groups: the group's pass rate is passed / cases over its categories.
SAFETY_GROUPS: dict[str, tuple[str, ...]] = {"injection": ("injection_direct", "injection_indirect")}


class PolicyError(ValueError):
    pass


class RegressionError(ValueError):
    """The two runs can't be compared (e.g. different dataset versions)."""


# -- metrics ------------------------------------------------------------------------


def _safety_metric(metric: str) -> str | None:
    """'safety.<category or group>.pass_rate' -> the category or group; None if not a safety metric."""
    if not metric.startswith("safety."):
        return None
    name, dot, stat = metric.removeprefix("safety.").rpartition(".")
    known = [*ATTACK_CATEGORIES, *SAFETY_GROUPS]
    if not dot or stat != "pass_rate" or name not in known:
        raise PolicyError(f"unknown safety metric '{metric}' (known: safety.<{' | '.join(known)}>.pass_rate)")
    return name


def metric_direction(metric: str) -> Direction:
    """Direction of a policy metric name; raises PolicyError for unknown names."""
    if metric in RUN_METRICS:
        return RUN_METRICS[metric]
    if _safety_metric(metric) is not None:
        return "higher"
    name, dot, stat = metric.rpartition(".")
    if not dot or stat not in EVALUATOR_STATS or not name:
        known = ", ".join([*RUN_METRICS, *(f"<evaluator>.{s}" for s in EVALUATOR_STATS)])
        raise PolicyError(f"unknown metric '{metric}' (known: {known})")
    try:
        resolve(name)
    except UnknownEvaluatorError as exc:
        raise PolicyError(f"metric '{metric}': {exc}") from exc
    return EVALUATOR_STATS[stat]


def _is_fraction(metric: str) -> bool:
    return metric in FRACTION_METRICS or metric.endswith((".mean_score", ".pass_rate"))


# -- policy -------------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    minimums: dict[str, float] = field(default_factory=dict)
    maximums: dict[str, float] = field(default_factory=dict)
    regressions: dict[str, dict[str, float]] = field(default_factory=dict)
    max_newly_failing: int | None = None
    no_newly_failing_tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        cases: dict[str, Any] = {}
        if self.max_newly_failing is not None:
            cases["max_newly_failing"] = self.max_newly_failing
        if self.no_newly_failing_tags:
            cases["no_newly_failing_tags"] = list(self.no_newly_failing_tags)
        out: dict[str, Any] = {}
        for key, value in (
            ("minimums", self.minimums),
            ("maximums", self.maximums),
            ("regressions", self.regressions),
            ("cases", cases),
        ):
            if value:
                out[key] = value
        return out


def _number(where: str, value: Any, *, non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PolicyError(f"{where} must be a number")
    if non_negative and value < 0:
        raise PolicyError(f"{where} must be >= 0")
    return float(value)


def _mapping(where: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyError(f"{where} must be a mapping")
    return value


def _bound(section: str, raw: Any, want: Direction) -> dict[str, float]:
    out: dict[str, float] = {}
    for metric, value in _mapping(section, raw).items():
        where = f"{section}.{metric}"
        direction = metric_direction(str(metric))
        if direction != want:
            other = "maximums" if section == "minimums" else "minimums"
            raise PolicyError(
                f"{where}: {metric} is {direction}-is-better, so a {section[:-1]} doesn't make sense (use {other})"
            )
        number = _number(where, value)
        if _is_fraction(str(metric)) and not 0 <= number <= 1:
            raise PolicyError(f"{where} must be between 0 and 1 ({metric} is a fraction)")
        out[str(metric)] = number
    return out


def _regressions(raw: Any) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for metric, rule in _mapping("regressions", raw).items():
        where = f"regressions.{metric}"
        direction = metric_direction(str(metric))
        allowed = REGRESSION_KEYS[direction]
        rule = _mapping(where, rule)
        if not rule:
            raise PolicyError(f"{where} needs one of {list(allowed)}")
        bad = sorted(set(rule) - set(allowed))
        if bad:
            raise PolicyError(
                f"{where}: {metric} is {direction}-is-better; use {' / '.join(allowed)}, not {', '.join(bad)}"
            )
        out[str(metric)] = {k: _number(f"{where}.{k}", v, non_negative=True) for k, v in rule.items()}
    return out


def parse_policy(raw: Any) -> Policy:
    """Validate a `release_policy` block. Raises PolicyError naming the key."""
    raw = _mapping("release_policy", raw)
    unknown = sorted(set(raw) - {"minimums", "maximums", "regressions", "cases"})
    if unknown:
        raise PolicyError(
            f"release_policy: unknown key(s) {unknown} (accepted: minimums, maximums, regressions, cases)"
        )
    cases = _mapping("cases", raw.get("cases") or {})
    unknown = sorted(set(cases) - {"max_newly_failing", "no_newly_failing_tags"})
    if unknown:
        raise PolicyError(f"cases: unknown key(s) {unknown} (accepted: max_newly_failing, no_newly_failing_tags)")
    max_newly = cases.get("max_newly_failing")
    if max_newly is not None and (isinstance(max_newly, bool) or not isinstance(max_newly, int) or max_newly < 0):
        raise PolicyError("cases.max_newly_failing must be an integer >= 0")
    tags = cases.get("no_newly_failing_tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
        raise PolicyError("cases.no_newly_failing_tags must be a list of tag names")
    policy = Policy(
        minimums=_bound("minimums", raw.get("minimums") or {}, "higher"),
        maximums=_bound("maximums", raw.get("maximums") or {}, "lower"),
        regressions=_regressions(raw.get("regressions") or {}),
        max_newly_failing=max_newly,
        no_newly_failing_tags=tuple(tags),
    )
    if not policy.to_dict():
        raise PolicyError("release_policy defines no checks: a gate with nothing to check would always pass")
    return policy


# -- run snapshots + regression -----------------------------------------------------


@dataclass(frozen=True)
class CaseOutcome:
    status: str  # ok | error | timeout
    passed: bool
    tags: tuple[str, ...] = ()
    failed_evaluators: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunSnapshot:
    """What the comparison needs from one finished run: its stored aggregates
    and each case's verdict, keyed by case_key."""

    run_id: str
    dataset_version_id: str
    aggregates: Mapping[str, Any]
    cases: Mapping[str, CaseOutcome]


def _evaluator_entry(agg: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for entry in (agg.get("metrics") or {}).values():
        if entry.get("name") == name:
            return entry
    return None


def safety_pass_rate(aggregates: Mapping[str, Any], name: str) -> float | None:
    """A category's (or pooled group's) case pass rate; None if the run has none of its cases."""
    by_category = (aggregates.get("safety") or {}).get("by_category") or {}
    entries = [by_category[c] for c in SAFETY_GROUPS.get(name, (name,)) if c in by_category]
    cases = sum(e["cases"] for e in entries)
    return sum(e["passed"] for e in entries) / cases if cases else None


def metric_value(snapshot: RunSnapshot, metric: str) -> float | None:
    agg = snapshot.aggregates
    safety_name = _safety_metric(metric)
    if safety_name is not None:
        return safety_pass_rate(agg, safety_name)
    cases = agg.get("case_count") or 0
    if metric == "pass_rate":
        return agg.get("pass_rate")
    if metric == "error_rate":
        return ((agg.get("error_count") or 0) + (agg.get("timeout_count") or 0)) / cases if cases else None
    if metric in ("p50_latency_ms", "p95_latency_ms"):
        return (agg.get("latency_ms") or {}).get(metric.split("_")[0])
    if metric == "estimated_cost_usd":
        return (agg.get("estimated_cost_usd") or {}).get("total")
    name, _, stat = metric.rpartition(".")
    entry = _evaluator_entry(agg, name)
    return None if entry is None else entry.get(stat)


def _evaluator_version(snapshot: RunSnapshot, metric: str) -> str | None:
    if metric in RUN_METRICS or metric.startswith("safety."):
        return None
    entry = _evaluator_entry(snapshot.aggregates, metric.rpartition(".")[0])
    return None if entry is None else entry.get("version")


def delta(baseline: float | None, candidate: float | None) -> dict[str, float | None]:
    """{baseline, candidate, delta (candidate - baseline), delta_pct (of |baseline|)}."""
    d = None if baseline is None or candidate is None else candidate - baseline
    pct = None if d is None or not baseline else d / abs(baseline) * 100
    return {"baseline": baseline, "candidate": candidate, "delta": d, "delta_pct": pct}


def compare(baseline: RunSnapshot, candidate: RunSnapshot) -> dict[str, Any]:
    """The regression report: run-level and per-evaluator deltas, and every
    case classified newly_failing / fixed / still_failing / still_passing."""
    if baseline.dataset_version_id != candidate.dataset_version_id:
        raise RegressionError(
            f"runs used different dataset versions ({baseline.dataset_version_id} vs "
            f"{candidate.dataset_version_id}); a regression comparison needs the same cases"
        )
    summary = {m: delta(metric_value(baseline, m), metric_value(candidate, m)) for m in RUN_METRICS}

    names = sorted({e["name"] for s in (baseline, candidate) for e in (s.aggregates.get("metrics") or {}).values()})
    metrics = []
    for name in names:
        b, c = _evaluator_entry(baseline.aggregates, name), _evaluator_entry(candidate.aggregates, name)
        bv, cv = (b or {}).get("version"), (c or {}).get("version")
        metrics.append(
            {
                "name": name,
                "unit": (c or b or {}).get("unit"),
                "baseline_version": bv,
                "candidate_version": cv,
                "comparable": b is not None and c is not None and bv == cv,
                **{stat: delta((b or {}).get(stat), (c or {}).get(stat)) for stat in EVALUATOR_STATS},
            }
        )

    categories = sorted(
        {c for s in (baseline, candidate) for c in ((s.aggregates.get("safety") or {}).get("by_category") or {})}
    )
    safety = {
        c: delta(safety_pass_rate(baseline.aggregates, c), safety_pass_rate(candidate.aggregates, c))
        for c in categories
    }

    classes: dict[str, list[dict[str, Any]]] = {k: [] for k in CASE_CLASSES}
    for key in sorted(set(baseline.cases) | set(candidate.cases)):
        b_case, c_case = baseline.cases.get(key), candidate.cases.get(key)
        if b_case is None or c_case is None:  # same dataset version, so only if a run is partial
            continue
        cls = {
            (True, False): "newly_failing",
            (False, True): "fixed",
            (False, False): "still_failing",
            (True, True): "still_passing",
        }[(b_case.passed, c_case.passed)]
        classes[cls].append(
            {
                "case_key": key,
                "tags": list(c_case.tags),
                "baseline_status": b_case.status,
                "candidate_status": c_case.status,
                "candidate_failed_evaluators": list(c_case.failed_evaluators),
            }
        )
    return {
        "baseline_run_id": baseline.run_id,
        "candidate_run_id": candidate.run_id,
        "dataset_version_id": candidate.dataset_version_id,
        "summary": summary,
        "metrics": metrics,
        # Per attack category, for adversarial datasets (empty otherwise).
        "safety": safety,
        "cases": classes,
        "case_counts": {k: len(v) for k, v in classes.items()},
    }


# -- gate checks ------------------------------------------------------------------


def _check(
    kind: str,
    metric: str,
    rule: str,
    threshold: float | None,
    values: Mapping[str, float | None],
    passed: bool,
    reason: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "metric": metric,
        "rule": rule,
        "threshold": threshold,
        "baseline": values.get("baseline"),
        "candidate": values.get("candidate"),
        "delta": values.get("delta"),
        "delta_pct": values.get("delta_pct"),
        "passed": passed,
        "reason": reason,
    }


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def evaluate_policy(policy: Policy, baseline: RunSnapshot, candidate: RunSnapshot) -> list[dict[str, Any]]:
    """Every check the policy defines, in policy order. A value that wasn't
    measured fails its check: a gate can't pass on a number it doesn't have."""
    report = compare(baseline, candidate)
    checks: list[dict[str, Any]] = []

    def values(metric: str) -> dict[str, float | None]:
        return delta(metric_value(baseline, metric), metric_value(candidate, metric))

    for metric, threshold in policy.minimums.items():
        v = values(metric)
        c = v["candidate"]
        ok = c is not None and c >= threshold - EPSILON
        reason = (
            f"{metric} not measured in the candidate" if c is None else f"{_fmt(c)} {'>=' if ok else '<'} {threshold:g}"
        )
        checks.append(_check("minimum", metric, f">= {threshold:g}", threshold, v, ok, reason))

    for metric, threshold in policy.maximums.items():
        v = values(metric)
        c = v["candidate"]
        ok = c is not None and c <= threshold + EPSILON
        reason = (
            f"{metric} not measured in the candidate" if c is None else f"{_fmt(c)} {'<=' if ok else '>'} {threshold:g}"
        )
        checks.append(_check("maximum", metric, f"<= {threshold:g}", threshold, v, ok, reason))

    for metric, rule in policy.regressions.items():
        direction = metric_direction(metric)
        v = values(metric)
        b, c = v["baseline"], v["candidate"]
        b_ver, c_ver = _evaluator_version(baseline, metric), _evaluator_version(candidate, metric)
        for key, limit in rule.items():
            pct = key.endswith("_pct")
            word = "drop" if direction == "higher" else "increase"
            rule_text = f"{word} <= {limit:g}{'%' if pct else ''}"
            if b is None or c is None:
                missing = "baseline" if b is None else "candidate"
                checks.append(
                    _check("regression", metric, rule_text, limit, v, False, f"{metric} not measured in the {missing}")
                )
                continue
            if b_ver != c_ver:
                checks.append(
                    _check(
                        "regression", metric, rule_text, limit, v, False,
                        f"evaluator versions differ ({b_ver} vs {c_ver}): not comparable",
                    )
                )  # fmt: skip
                continue
            worsening = (b - c) if direction == "higher" else (c - b)
            if pct:
                if b == 0:
                    ok = worsening <= EPSILON
                    amount = "n/a (baseline is 0)" if not ok else "0%"
                else:
                    share = worsening / abs(b) * 100
                    ok = share <= limit + EPSILON
                    amount = f"{share:.3g}%"
            else:
                ok = worsening <= limit + EPSILON
                amount = f"{worsening:.6g}"
            verdict = "within" if ok else "exceeds"
            reason = f"{word} {amount} ({_fmt(b)} -> {_fmt(c)}) {verdict} {limit:g}{'%' if pct else ''}"
            if worsening < 0:
                reason = f"improved ({_fmt(b)} -> {_fmt(c)})"
            checks.append(_check("regression", metric, rule_text, limit, v, ok, reason))

    newly = report["cases"]["newly_failing"]
    count_values = {"baseline": None, "candidate": float(len(newly)), "delta": None, "delta_pct": None}
    if policy.max_newly_failing is not None:
        limit_n = policy.max_newly_failing
        ok = len(newly) <= limit_n
        keys = ", ".join(c["case_key"] for c in newly) or "none"
        checks.append(
            _check(
                "cases", "newly_failing_cases", f"<= {limit_n}", float(limit_n), count_values, ok,
                f"{len(newly)} newly failing ({keys})",
            )
        )  # fmt: skip
    for tag in policy.no_newly_failing_tags:
        tagged = [c["case_key"] for c in newly if tag in c["tags"]]
        tagged_values = {**count_values, "candidate": float(len(tagged))}
        reason = f"newly failing '{tag}' case(s): {', '.join(tagged)}" if tagged else f"no newly failing '{tag}' case"
        checks.append(_check("cases", f"newly_failing[tag={tag}]", "== 0", 0.0, tagged_values, not tagged, reason))
    return checks
