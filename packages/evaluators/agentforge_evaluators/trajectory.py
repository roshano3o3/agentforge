"""Deterministic trajectory evaluators over an agent's recorded steps.

Every check here compares the steps the agent *reported* (tool names, args,
results, in order) against the test case's `trajectory` expectations. None
of them judges whether a step was wise -- they check what the dataset
author declared. Step numbers in reasons and in `evidence["failing_steps"]`
are 1-based positions in the full step list (retrieval, tool calls and the
final answer all count), the same numbering the dashboard shows.

Per-case trajectory expectations (the test case's `trajectory` block; each
evaluator's own params can override its key):

    expected_tools:           [tool, ...]                    -> tool_selection
    forbidden_tools:          [tool, ...]                    -> forbidden_tool_use
    expected_sequence:        {tools: [...], mode: strict|subsequence} -> sequence_order
    expected_args:            [{tool, args: {...}} | {tool, schema: {...}}] -> tool_args
    requires_approval_before: [tool, ...]  (+ approval_tool) -> approval_required
    max_identical_calls:      int (default 2)                -> loop_detection
    max_steps:                int                            -> step_limit
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from agentforge_evaluators.base import EvalConfig, EvalInput, MetricOutcome, Params, TrajectoryStep, not_applicable

DEFAULT_APPROVAL_TOOL = "request_human_approval"
DEFAULT_MAX_IDENTICAL_CALLS = 2
SEQUENCE_MODES = ("strict", "subsequence")


class TrajectoryConfigError(ValueError):
    pass


# -- expectations: validation --------------------------------------------------


def _tool_list(key: str, value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise TrajectoryConfigError(f"{key} must be a list of tool names")
    if not value and not allow_empty:
        raise TrajectoryConfigError(f"{key} must not be empty")
    return list(value)


def _positive_int(key: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise TrajectoryConfigError(f"{key} must be an integer >= 1")
    return value


def _sequence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"tools", "mode"}:
        raise TrajectoryConfigError("expected_sequence must be {tools: [...], mode: strict|subsequence}")
    mode = value.get("mode")
    if mode not in SEQUENCE_MODES:
        raise TrajectoryConfigError(f"expected_sequence.mode must be one of {list(SEQUENCE_MODES)}")
    return {"tools": _tool_list("expected_sequence.tools", value.get("tools")), "mode": mode}


def _expected_args(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise TrajectoryConfigError("expected_args must be a non-empty list of {tool, args} or {tool, schema}")
    specs = []
    for i, spec in enumerate(value):
        where = f"expected_args[{i}]"
        if not isinstance(spec, Mapping) or not isinstance(spec.get("tool"), str) or not spec["tool"].strip():
            raise TrajectoryConfigError(f"{where} needs a 'tool' name")
        extra = set(spec) - {"tool", "args", "schema"}
        if extra:
            raise TrajectoryConfigError(f"{where} has unknown key(s) {sorted(extra)}")
        if ("args" in spec) == ("schema" in spec):
            raise TrajectoryConfigError(f"{where} needs exactly one of 'args' (exact match) or 'schema' (JSON Schema)")
        if "args" in spec:
            if not isinstance(spec["args"], Mapping):
                raise TrajectoryConfigError(f"{where}.args must be an object")
            specs.append({"tool": spec["tool"], "args": dict(spec["args"])})
        else:
            try:
                Draft202012Validator.check_schema(spec["schema"])
            except SchemaError as exc:
                raise TrajectoryConfigError(f"{where}.schema is not a valid JSON Schema: {exc.message}") from exc
            specs.append({"tool": spec["tool"], "schema": dict(spec["schema"])})
    return specs


_VALIDATORS = {
    "expected_tools": lambda v: _tool_list("expected_tools", v, allow_empty=True),
    "forbidden_tools": lambda v: _tool_list("forbidden_tools", v),
    "expected_sequence": _sequence,
    "expected_args": _expected_args,
    "requires_approval_before": lambda v: _tool_list("requires_approval_before", v),
    "approval_tool": lambda v: _tool_list("approval_tool", [v])[0],
    "max_identical_calls": lambda v: _positive_int("max_identical_calls", v),
    "max_steps": lambda v: _positive_int("max_steps", v),
}
TRAJECTORY_KEYS = frozenset(_VALIDATORS)


def validate_trajectory(raw: Any, *, allowed: frozenset[str] = TRAJECTORY_KEYS) -> dict[str, Any] | None:
    """Validate and normalize a trajectory expectations block (or the subset
    of it an evaluator accepts as params). None stays None."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TrajectoryConfigError("trajectory must be a mapping")
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise TrajectoryConfigError(f"unknown key(s) {unknown} (accepted: {sorted(allowed)})")
    return {key: _VALIDATORS[key](value) for key, value in raw.items()}


# -- helpers ------------------------------------------------------------------------


def _expect(case: EvalInput, params: Params, key: str) -> Any:
    """An evaluator's param overrides the case's trajectory block."""
    if key in params:
        return params[key]
    return (case.trajectory or {}).get(key)


def _calls(case: EvalInput) -> list[TrajectoryStep]:
    return [s for s in case.steps if s.kind == "tool_call"]


def _steps(steps: Sequence[TrajectoryStep]) -> str:
    return ", ".join(str(s.index) for s in steps)


def _outcome(passed: bool, reason: str, evidence: dict[str, Any], score: float | None = None) -> MetricOutcome:
    return MetricOutcome(
        score=(1.0 if passed else 0.0) if score is None else score,
        passed=passed,
        reason=reason,
        evidence=evidence,
    )


# -- evaluators -----------------------------------------------------------------------


def tool_selection(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    expected_raw = _expect(case, params, "expected_tools")
    if expected_raw is None:
        return not_applicable("no expected_tools for this case")
    expected = set(expected_raw)
    calls = _calls(case)
    called = {s.name for s in calls}
    unexpected = [s for s in calls if s.name not in expected]
    missing = sorted(expected - called)

    if not called and not expected:
        return _outcome(True, "no tools called and none expected", {"expected_tools": [], "called_tools": []})
    precision = len(called & expected) / len(called) if called else 0.0
    recall = len(called & expected) / len(expected) if expected else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    passed = precision >= config.threshold and recall >= config.threshold

    parts = [f"precision {precision:.2f}, recall {recall:.2f}"]
    if unexpected:
        parts.append("unexpected: " + ", ".join(f"{s.name} at step {s.index}" for s in unexpected))
    if missing:
        parts.append(f"never called: {', '.join(missing)}")
    parts.append(f"{'both >=' if passed else 'below'} threshold {config.threshold}")
    return _outcome(
        passed,
        "; ".join(parts),
        {
            "formula": "precision = |called ∩ expected| / |called|; recall = |called ∩ expected| / |expected| "
            "(distinct tool names); score = F1",
            "expected_tools": sorted(expected),
            "called_tools": sorted(called),
            "precision": precision,
            "recall": recall,
            "missing_tools": missing,
            "failing_steps": [s.index for s in unexpected],
        },
        score=f1,
    )


def forbidden_tool_use(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    forbidden_raw = _expect(case, params, "forbidden_tools")
    if forbidden_raw is None:
        return not_applicable("no forbidden_tools for this case")
    forbidden = set(forbidden_raw)
    violations = [s for s in _calls(case) if s.name in forbidden]
    if not violations:
        return _outcome(True, f"none of {sorted(forbidden)} called", {"forbidden_tools": sorted(forbidden)})
    reason = "; ".join(f"{s.name} (forbidden) called at step {s.index}" for s in violations)
    return _outcome(
        False,
        reason,
        {"forbidden_tools": sorted(forbidden), "failing_steps": [s.index for s in violations]},
    )


def sequence_order(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    spec = _expect(case, params, "expected_sequence")
    if spec is None:
        return not_applicable("no expected_sequence for this case")
    expected: list[str] = list(spec["tools"])
    mode = spec["mode"]
    calls = _calls(case)
    actual = [s.name for s in calls]
    evidence: dict[str, Any] = {"mode": mode, "expected_sequence": expected, "actual_sequence": actual}

    if mode == "strict":
        if actual == expected:
            return _outcome(True, f"tool calls match {expected} exactly", evidence)
        i = next((k for k in range(min(len(actual), len(expected))) if actual[k] != expected[k]), None)
        if i is not None:
            step = calls[i]
            reason = f"tool call {i + 1} is {step.name} (step {step.index}); expected {expected[i]}"
            evidence["failing_steps"] = [step.index]
        elif len(actual) > len(expected):
            extra = calls[len(expected) :]
            reason = f"{len(extra)} extra tool call(s) after the expected sequence: " + ", ".join(
                f"{s.name} at step {s.index}" for s in extra
            )
            evidence["failing_steps"] = [s.index for s in extra]
        else:
            reason = f"sequence stopped early: missing {expected[len(actual) :]}"
            evidence["failing_steps"] = []
        return _outcome(False, reason, evidence)

    # subsequence: every expected tool, in order, with anything allowed in between.
    k = 0
    matched: list[int] = []
    for s in calls:
        if k < len(expected) and s.name == expected[k]:
            matched.append(s.index)
            k += 1
    evidence["matched_steps"] = matched
    if k == len(expected):
        return _outcome(True, f"{expected} occur in order (steps {', '.join(map(str, matched))})", evidence)
    missing = expected[k]
    after = f" after {expected[k - 1]} (step {matched[-1]})" if k else ""
    # Point at the first call that jumped ahead of the missing tool, if any.
    later = set(expected[k + 1 :])
    jumped = next((s for s in calls if s.name in later and (not matched or s.index > matched[-1])), None)
    reason = f"{missing} never called{after}"
    if jumped is not None:
        reason += f"; {jumped.name} at step {jumped.index} came without it"
    evidence["failing_steps"] = [jumped.index] if jumped is not None else []
    return _outcome(False, reason, evidence)


def tool_args(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    specs = _expect(case, params, "expected_args")
    if specs is None:
        return not_applicable("no expected_args for this case")
    calls = _calls(case)
    problems: list[str] = []
    failing: list[int] = []
    satisfied = 0
    checks: list[dict[str, Any]] = []
    for spec in specs:
        tool = spec["tool"]
        tool_calls = [s for s in calls if s.name == tool]
        if not tool_calls:
            problems.append(f"{tool} never called")
            checks.append({"tool": tool, "ok": False, "problem": "never called"})
            continue
        spec_problems = []
        for s in tool_calls:
            if "args" in spec:
                if dict(s.args) != spec["args"]:
                    spec_problems.append(f"{tool} at step {s.index} got {json.dumps(dict(s.args), sort_keys=True)}")
                    failing.append(s.index)
            else:
                error = next(iter(Draft202012Validator(spec["schema"]).iter_errors(dict(s.args))), None)
                if error is not None:
                    path = "/".join(str(p) for p in error.absolute_path) or "(args)"
                    spec_problems.append(f"{tool} at step {s.index}: {path}: {error.message}")
                    failing.append(s.index)
        checks.append({"tool": tool, "ok": not spec_problems, "match": "exact" if "args" in spec else "schema"})
        if spec_problems:
            problems.extend(spec_problems)
        else:
            satisfied += 1
    passed = not problems
    reason = f"all {len(specs)} argument expectation(s) met" if passed else "; ".join(problems)
    return _outcome(
        passed,
        reason,
        {
            "rule": "every call of an expected tool must match its expectation",
            "expectations": list(specs),
            "checks": checks,
            "failing_steps": sorted(set(failing)),
        },
        score=satisfied / len(specs),
    )


def _approved(step: TrajectoryStep) -> bool:
    """An approval step counts only if it didn't error and didn't explicitly
    say approved=false."""
    if step.error:
        return False
    if isinstance(step.result, Mapping) and "approved" in step.result:
        return bool(step.result["approved"])
    return True


def approval_required(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    guarded_raw = _expect(case, params, "requires_approval_before")
    if guarded_raw is None:
        return not_applicable("no requires_approval_before for this case")
    guarded = set(guarded_raw)
    approval_tool = _expect(case, params, "approval_tool") or DEFAULT_APPROVAL_TOOL
    approval_available: TrajectoryStep | None = None
    last_denied: TrajectoryStep | None = None
    violations: list[str] = []
    failing: list[int] = []
    guarded_calls = 0
    for s in _calls(case):
        if s.name == approval_tool:
            if _approved(s):
                approval_available = s
            else:
                last_denied = s
        elif s.name in guarded:
            guarded_calls += 1
            if approval_available is None:
                why = (
                    f"after {approval_tool} at step {last_denied.index} was denied"
                    if last_denied is not None
                    else f"with no prior {approval_tool}"
                )
                violations.append(f"{s.name} called at step {s.index} {why}")
                failing.append(s.index)
            approval_available = None  # one approval covers one guarded action
    evidence = {
        "requires_approval_before": sorted(guarded),
        "approval_tool": approval_tool,
        "rule": "each call to a guarded tool needs its own earlier, successful approval",
        "failing_steps": failing,
    }
    if violations:
        return _outcome(False, "; ".join(violations), evidence)
    if not guarded_calls:
        return _outcome(True, f"{', '.join(sorted(guarded))} never called", evidence)
    return _outcome(True, f"every call to {sorted(guarded)} was preceded by an approved {approval_tool}", evidence)


def loop_detection(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    limit = _expect(case, params, "max_identical_calls") or DEFAULT_MAX_IDENTICAL_CALLS
    calls = _calls(case)
    if not calls:
        return _outcome(True, "no tool calls", {"max_identical_calls": limit})
    keys = [(s.name, json.dumps(dict(s.args), sort_keys=True, default=str)) for s in calls]
    counts = Counter(keys)
    loops = {key: n for key, n in counts.items() if n > limit}
    if not loops:
        return _outcome(
            True,
            f"no tool called more than {limit} time(s) with identical args",
            {"max_identical_calls": limit, "most_repeated": counts.most_common(1)[0][1]},
        )
    problems = []
    failing: list[int] = []
    for (name, args), n in loops.items():
        repeated = [s for s, key in zip(calls, keys, strict=True) if key == (name, args)]
        problems.append(f"{name} called {n} times with identical args (steps {_steps(repeated)}); limit {limit}")
        failing.extend(s.index for s in repeated[limit:])
    return _outcome(False, "; ".join(problems), {"max_identical_calls": limit, "failing_steps": failing})


def step_limit(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    limit = _expect(case, params, "max_steps")
    if limit is None:
        return not_applicable("no max_steps for this case")
    actions = [s for s in case.steps if s.kind != "final_answer"]
    evidence: dict[str, Any] = {"max_steps": limit, "counted": "retrieval + tool_call steps (not the final answer)"}
    if len(actions) <= limit:
        outcome = _outcome(True, f"{len(actions)} step(s) <= limit {limit}", evidence)
    else:
        over = actions[limit:]
        evidence["failing_steps"] = [s.index for s in over]
        outcome = _outcome(
            False, f"{len(actions)} steps > limit {limit} (over the limit from step {over[0].index})", evidence
        )
    outcome.value = float(len(actions))
    outcome.unit = "steps"
    return outcome
