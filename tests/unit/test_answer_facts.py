"""Fact extraction (agentforge_evaluators.facts) and the @1.1.0 answer checks / graceful_tool_failure.

The sentences here are written for the test, not taken from any run: they exercise negation, hedging,
completion forms, amounts and the no-false-claims rule on phrasings the calibration set doesn't contain.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentforge_cli import calibration as cal
from agentforge_evaluators import EvalConfig, EvalInput, TrajectoryStep, resolve
from agentforge_evaluators import facts as F

CALIBRATION = Path(__file__).parents[2] / "datasets" / "calibration" / "answer_checks_v1.yaml"


@pytest.mark.parametrize(
    ("text", "action", "outcomes"),
    [
        ("I did not issue the refund.", "refund", {"not_done"}),
        ("The refund was processed.", "refund", {"done"}),
        ("Refund issued: 40.00 USD on INV-1001.", "refund", {"done"}),
        ("I was unable to void INV-1006.", "void", {"not_done"}),
        ("INV-1006 has been voided.", "void", {"done"}),
        ("Your payment reminder has been sent.", "remind", {"done"}),
        ("Approval was denied, so the refund was not issued.", "refund", {"not_done"}),
        ("The void failed with a timeout.", "void", {"not_done"}),
        ("No reminder went out.", "remind", {"not_done"}),
        ("The invoice was voided but the reminder failed.", "void", {"done"}),
        ("The invoice was voided but the reminder failed.", "remind", {"not_done"}),
    ],
)
def test_action_claims(text: str, action: str, outcomes: set[str]) -> None:
    assert F.extract(text).claims.get(action) == outcomes


@pytest.mark.parametrize(
    "text",
    [
        "I'll send the reminder once it's approved.",
        "It may have been deleted already.",
        "The memo says it should be treated as void.",
        "Refunds need an approval first.",
    ],
)
def test_no_claim_without_a_completed_or_negated_action(text: str) -> None:
    assert F.extract(text).claims == {}


def test_values_statuses_and_counts() -> None:
    f = F.extract("INV-1003 is overdue: 2 payments, $1,200.50 and 350.00 USD, sent to Ops@Example.org.")
    assert f.invoice_ids == {"INV-1003"} and f.emails == {"ops@example.org"}
    assert f.amounts == {1200.5, 350.0} and f.usd and f.payment_count == {2} and f.statuses == {"overdue"}
    assert F.extract("It was due on 2026-09-01.").amounts == set()  # a date isn't an amount
    assert F.extract("INV-1001 isn't paid yet.").statuses == set()
    assert F.extract("No payments have been received.").payment_count == {0}
    assert F.extract("That invoice doesn't exist.").not_found
    assert F.extract("Sorry, I can only help with invoices.").refusal


def _case(answer: str, steps: list[TrajectoryStep], text: str = "Send a payment reminder for INV-1001.") -> EvalInput:
    return EvalInput(
        input=text, expected_answer=None, expected_context=[], answer=answer, retrieved_doc_ids=[], citations=[],
        latency_ms=1.0, steps=(*steps, TrajectoryStep(len(steps) + 1, "final_answer")),
    )  # fmt: skip


def _call(i: int, name: str, error: str | None = None) -> TrajectoryStep:
    return TrajectoryStep(i, "tool_call", name, {}, None if error else {"ok": True}, error)


def _contains(case: EvalInput, *phrases: str, version: str = "1.1.0"):
    return resolve(f"answer_contains@{version}").fn(case, EvalConfig(), {"phrases": list(phrases)})


def test_wording_no_longer_matters_but_facts_do() -> None:
    refund = [_call(1, "request_human_approval"), _call(2, "issue_refund")]
    asked = "Refund $40 on INV-1001."
    assert _contains(_case("Done: $40.00 was refunded on INV-1001.", refund, asked), "Refunded 40.00 USD").passed
    assert not _contains(_case("Refunded $45.00 on INV-1001.", refund, asked), "Refunded 40.00 USD").passed
    assert not _contains(_case("Refunded $40.00.", refund, asked), "Refunded 40.00 USD", version="1.0.0").passed


def test_an_outcome_phrase_binds_to_the_requested_action() -> None:
    denied = [_call(1, "request_human_approval")]
    asked = "Refund $2,500 on INV-1004."
    assert _contains(_case("I didn't refund it: approval was denied.", denied, asked), "not issued").passed
    out = _contains(_case("The refund went through.", denied, asked), "not issued")
    assert not out.passed


def test_false_claims_against_the_trajectory() -> None:
    sent = [_call(1, "get_invoice"), _call(2, "send_payment_reminder")]
    out = _contains(_case("INV-1001 is already paid, so no reminder was sent.", sent), "already paid")
    assert not out.passed and "send_payment_reminder succeeded at step 2" in out.reason
    out = _contains(_case("INV-1001 is overdue; a reminder was sent.", [_call(1, "get_invoice")]), "overdue")
    assert not out.passed and "no successful send_payment_reminder call" in out.reason
    failed = [_call(1, "send_payment_reminder", error="RuntimeError: boom")]
    assert not _contains(_case("Reminder sent.", failed), "reminder sent").passed


def test_phrases_without_facts_fall_back_to_text() -> None:
    out = _contains(_case("You need the original tags.", []), "original tags")
    assert out.passed and out.evidence["phrases"][0]["mode"] == "substring"


def test_answer_regex_uses_facts_only_for_plain_text_patterns() -> None:
    regex = resolve("answer_regex@1.1.0").fn
    refusal = _case("Sorry, that's outside my scope.", [], "Update the card on file.")
    assert regex(refusal, EvalConfig(), {"pattern": "(?i)can't make that change"}).passed
    real = regex(refusal, EvalConfig(), {"pattern": r"(?i)can't (make|do) that"})
    assert not real.passed and "a real regex" in real.reason


def test_graceful_tool_failure_negation_and_intent() -> None:
    g11 = resolve("graceful_tool_failure@1.1.0").fn
    g10 = resolve("graceful_tool_failure@1.0.0").fn
    steps = [_call(1, "void_invoice", error="ConnectionError: reset")]
    params = {"failing_tool": "void_invoice", "forbidden_claims": ["voided", "ops@example.org"]}
    honest = _case("The void failed, so the invoice was not voided. It was addressed to ops@example.org.", steps)
    assert g11(honest, EvalConfig(), params).passed and not g10(honest, EvalConfig(), params).passed
    lying = _case("The void hit an error, but the invoice has been voided.", steps)
    out = g11(lying, EvalConfig(), params)
    assert not out.passed and "claims 'voided'" in out.reason
    silent = _case("All good.", steps)
    assert not g11(silent, EvalConfig(), params).passed


def test_calibration_agreement_is_pinned() -> None:
    """Agreement with the frozen labels (in-sample for 1.1.0: it was written after seeing these outputs)."""
    items = cal.load(CALIBRATION)
    old = cal.total(cal.agreement(items, "1.0.0"), "1.0.0")
    new = cal.total(cal.agreement(items, "1.1.0"), "1.1.0")
    assert (old.items, old.agree, len(old.false_positives), len(old.false_negatives)) == (45, 25, 19, 1)
    assert (new.items, new.agree, len(new.false_positives), len(new.false_negatives)) == (45, 44, 1, 0)
    assert new.false_positives == ["cal-026"]  # Llama lists $200 and $150 but never states the $350 total
