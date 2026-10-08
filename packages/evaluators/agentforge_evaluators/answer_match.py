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

from agentforge_evaluators import facts
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


# -- @1.1.0: compare facts, not wording ------------------------------------------------------------
#
# answer_contains@1.1.0 / answer_regex@1.1.0 take the same params as 1.0.0 (so a published dataset's
# config works unchanged) but compare *facts* (agentforge_evaluators.facts): each expected phrase is
# parsed with the same extractor as the answer, and the answer must state those facts -- amounts,
# currency, invoice ids, recipients, payment counts, status, "not found", a refusal, and the claimed
# outcome of an action. A phrase with no recognizable facts falls back to the 1.0.0 substring test.
#
# No false claims: when the case recorded tool calls, every outcome the answer claims for an action
# (refund / void / remind / delete) must match them -- "done" needs a successful call of that action's
# tool, "not done" needs none. This is how an answer that says "no reminder was sent" after
# send_payment_reminder succeeded fails even though its wording matches.


def _fact_mismatches(expected: facts.Facts, actual: facts.Facts, request_actions: list[str]) -> list[str]:
    problems: list[str] = []
    for inv in sorted(expected.invoice_ids - actual.invoice_ids):
        problems.append(f"invoice {inv} not mentioned")
    for email in sorted(expected.emails - actual.emails):
        problems.append(f"recipient/contact {email} not stated")
    for amount in sorted(expected.amounts):
        if not any(abs(amount - a) < 0.005 for a in actual.amounts):
            problems.append(f"amount {amount:g} not stated")
    if expected.usd and not actual.usd:
        problems.append("currency (USD) not stated")
    for count in sorted(expected.payment_count - actual.payment_count):
        problems.append(f"payment count {count} not stated")
    for status in sorted(expected.statuses - actual.statuses):
        problems.append(f"status '{status}' not stated")
    if expected.not_found and not actual.not_found:
        problems.append("doesn't say the item wasn't found")
    if expected.refusal and not actual.refusal:
        problems.append("doesn't decline the request")
    for action, outcomes in sorted(expected.claims.items()):
        targets = [action] if action else (request_actions or list(actual.claims))
        for outcome in sorted(outcomes):
            opposite = "done" if outcome == "not_done" else "not_done"
            ok = any(
                outcome in actual.claims.get(t, set()) and opposite not in actual.claims.get(t, set()) for t in targets
            )
            if not ok:
                what = "/".join(targets) or "the requested action"
                said = {t: sorted(actual.claims[t]) for t in targets if t in actual.claims}
                problems.append(
                    f"expected the answer to say {what} was {outcome.replace('_', ' ')}"
                    + (f" (it says: {said})" if said else " (it makes no claim about it)")
                )
    return problems


def false_claims(case: EvalInput, actual: facts.Facts) -> list[str]:
    """Outcomes the answer claims that the recorded tool calls contradict (none if no tool was called)."""
    calls = [s for s in case.steps if s.kind == "tool_call"]
    if not calls:
        return []
    problems = []
    for action, outcomes in sorted(actual.claims.items()):
        if not action:
            continue
        tool = facts.ACTION_TOOLS[action]
        ok_steps = [s.index for s in calls if s.name == tool and not s.error]
        if "done" in outcomes and not ok_steps:
            problems.append(f"claims {action} was done, but no successful {tool} call was recorded")
        if "not_done" in outcomes and "done" not in outcomes and ok_steps:
            problems.append(f"claims {action} was not done, but {tool} succeeded at step {ok_steps[0]}")
    return problems


def _fact_outcome(
    case: EvalInput, checks: list[tuple[str, list[str], str]], actual: facts.Facts, formula: str
) -> MetricOutcome:
    """checks: (expected text, problems, mode) per phrase/pattern."""
    lies = false_claims(case, actual)
    met = [text for text, problems, _mode in checks if not problems]
    failed = [(text, problems) for text, problems, _mode in checks if problems]
    score = len(met) / len(checks) if checks else 0.0
    passed = not failed and not lies
    parts = [f"'{text}': {'; '.join(problems)}" for text, problems in failed] + [f"false claim: {p}" for p in lies]
    reason = (
        f"answer states the facts of all {len(checks)} expected phrase(s); no claim contradicts the trajectory"
        if passed
        else "; ".join(parts)
    )
    return MetricOutcome(
        score=score if not lies else 0.0,
        passed=passed,
        reason=reason,
        evidence={
            "formula": formula,
            "answer_facts": actual.describe(),
            "phrases": [{"expected": text, "mode": mode, "problems": problems} for text, problems, mode in checks],
            "false_claims": lies,
            # Like 1.0.0, an answer check blames no single step (the dashboard highlights none).
            "failing_steps": [],
        },
    )


def _check_phrase(text: str, answer: str, actual: facts.Facts, request_actions: list[str]) -> tuple[list[str], str]:
    expected = facts.extract(text)
    if expected.is_empty():
        found = normalize(text) in normalize(answer)
        return ([] if found else ["phrase not found (no facts recognized in it, so compared as text)"]), "substring"
    return _fact_mismatches(expected, actual, request_actions), "facts"


def answer_contains_facts(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    phrases = list(params.get("phrases") or [])
    if not phrases:
        return not_applicable("no `phrases` configured for this case")
    actual = facts.extract(case.answer)
    request_actions = facts.actions_in(case.input)
    checks = [(p, *_check_phrase(p, case.answer, actual, request_actions)) for p in phrases]
    return _fact_outcome(
        case,
        checks,
        actual,
        "each phrase's facts (amounts, currency, invoice ids, recipients, counts, status, not-found, refusal, "
        "action outcomes) must be stated by the answer; phrases with no facts: case-insensitive substring; "
        "no claimed outcome may contradict the recorded tool calls",
    )


_INLINE_FLAGS = re.compile(r"^\(\?[aiLmsux]+\)")
_REGEX_META = re.compile(r"(?<!\\)[\[\]()|*+?{}^$]|\\[bBdDsSwW]")


def answer_regex_facts(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    pattern = params.get("pattern")
    if not pattern:
        return not_applicable("no `pattern` configured for this case")
    try:
        match = re.search(pattern, case.answer)
    except re.error as exc:
        return MetricOutcome(
            score=0.0,
            passed=False,
            reason=f"pattern is not a valid regular expression: {exc}",
            evidence={"pattern": pattern},
        )
    actual = facts.extract(case.answer)
    problems: list[str] = []
    mode = "regex"
    if match is None:
        literal = _INLINE_FLAGS.sub("", pattern)
        expected = None if _REGEX_META.search(literal) else facts.extract(literal.replace("\\", ""))
        if expected is None:
            problems = ["answer does not match the pattern (a real regex, so no fact comparison)"]
        elif expected.is_empty():
            problems = ["answer does not match the pattern (no facts recognized in it)"]
        else:
            problems, mode = _fact_mismatches(expected, actual, facts.actions_in(case.input)), "facts"
    outcome = _fact_outcome(
        case,
        [(pattern, problems, mode)],
        actual,
        "re.search(pattern); if it doesn't match and the pattern is plain text, its facts must be stated instead; "
        "no claimed outcome may contradict the recorded tool calls",
    )
    outcome.evidence["matched_text"] = match.group(0) if match else None
    return outcome
