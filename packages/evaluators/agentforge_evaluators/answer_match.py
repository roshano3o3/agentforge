"""Deterministic answer-text evaluators: exact match, contains, regex.

None of these judge meaning -- they are string checks against what the
dataset author wrote down. A semantically correct answer phrased
differently fails `exact_match`; that is the metric's definition, not a bug.

Per-case params (from the dataset's evaluator config):
  exact_match      expected: str       (default: the case's expected_answer)
  answer_contains  phrases: [str, ...] (required)
  answer_regex     pattern: str        (required)
"""

from __future__ import annotations

import re

from agentforge_evaluators.base import EvalConfig, EvalInput, MetricOutcome, Params, not_applicable

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """casefold + trim + collapse internal whitespace. Nothing else (no
    punctuation stripping, no stemming) -- kept minimal so what counts as
    "exact" is easy to predict."""
    return _WHITESPACE.sub(" ", text.strip()).casefold()


def exact_match(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    expected_raw = params.get("expected", case.expected_answer)
    if expected_raw is None:
        return not_applicable("no expected answer (neither an `expected` param nor the case's expected_answer)")
    expected = normalize(expected_raw)
    actual = normalize(case.answer)
    matched = expected == actual
    return MetricOutcome(
        score=1.0 if matched else 0.0,
        passed=matched,
        reason=(
            "answer equals the expected answer (after casefold + whitespace normalization)"
            if matched
            else "answer differs from the expected answer (after casefold + whitespace normalization)"
        ),
        evidence={"normalization": "casefold, trim, collapse whitespace", "expected": expected, "actual": actual},
    )


def answer_contains(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    phrases = list(params.get("phrases") or [])
    if not phrases:
        return not_applicable("no `phrases` configured for this case")
    haystack = normalize(case.answer)
    found = [p for p in phrases if normalize(p) in haystack]
    missing = [p for p in phrases if normalize(p) not in haystack]
    score = len(found) / len(phrases)
    return MetricOutcome(
        score=score,
        passed=not missing,
        reason=(
            f"answer contains all {len(phrases)} expected phrase(s)"
            if not missing
            else f"answer is missing {len(missing)} of {len(phrases)} expected phrase(s): {missing}"
        ),
        evidence={
            "formula": "|phrases found| / |phrases| (case-insensitive substring)",
            "found": found,
            "missing": missing,
        },
    )


def answer_regex(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    pattern = params.get("pattern")
    if not pattern:
        return not_applicable("no `pattern` configured for this case")
    try:
        match = re.search(pattern, case.answer)
    except re.error as exc:
        # Configs are validated on the way in, so this only happens for data
        # written before that validation existed. Visible failure, not a skip.
        return MetricOutcome(
            score=0.0,
            passed=False,
            reason=f"pattern is not a valid regular expression: {exc}",
            evidence={"pattern": pattern},
        )
    return MetricOutcome(
        score=1.0 if match else 0.0,
        passed=match is not None,
        reason="answer matches the pattern" if match else "answer does not match the pattern",
        evidence={"pattern": pattern, "matched_text": match.group(0) if match else None, "semantics": "re.search"},
    )
