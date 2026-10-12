"""answer_grounded@1.0.0, on trajectories and answers written for the test (not from any run or calibration set),
and how it joins a run's evaluators (opt-in)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentforge_cli import calibration as cal
from agentforge_evaluators import (
    DEFAULT_EVALUATORS,
    OPT_IN_EVALUATORS,
    EvalConfig,
    EvalInput,
    TrajectoryStep,
    effective_config,
    resolve,
)

GROUNDED = resolve("answer_grounded@1.0.0").fn
INVOICE = {"id": "INV-2001", "customer_id": "CUST-07", "amount": 640.0, "status": "open", "currency": "USD"}
CUSTOMER = {"id": "CUST-07", "name": "Lumen Tile Works", "billing_email": "ar@lumentile.example"}
HISTORY = {"invoice_id": "INV-2001", "payments": [{"amount": 100.0}, {"amount": 40.0}]}


def _call(i: int, name: str, args: dict[str, Any], result: Any = None, error: str | None = None) -> TrajectoryStep:
    return TrajectoryStep(index=i, kind="tool_call", name=name, args=args, result=result, error=error)


def _case(answer: str, steps: list[TrajectoryStep], text: str = "What's going on with INV-2001?") -> EvalInput:
    final = TrajectoryStep(index=len(steps) + 1, kind="final_answer", name="", args={}, result=None, error=None)
    return EvalInput(
        input=text,
        expected_answer=None,
        expected_context=[],
        answer=answer,
        retrieved_doc_ids=[],
        citations=[],
        latency_ms=0.0,
        steps=(*steps, final),
    )


LOOKUPS = [
    _call(1, "get_invoice", {"invoice_id": "INV-2001"}, INVOICE),
    _call(2, "get_customer", {"customer_id": "CUST-07"}, CUSTOMER),
    _call(3, "get_payment_history", {"invoice_id": "INV-2001"}, HISTORY),
]


def _run(answer: str, steps: list[TrajectoryStep] = LOOKUPS, text: str = "What's going on with INV-2001?"):
    return GROUNDED(_case(answer, steps, text), EvalConfig(), {})


def test_an_answer_built_from_the_trajectory_passes() -> None:
    out = _run(
        "INV-2001 for Lumen Tile Works is open: 640.00 USD, with two payments totalling $140 and $500 left. "
        "The billing contact is ar@lumentile.example."
    )
    assert out.passed, out.reason


@pytest.mark.parametrize(
    ("answer", "needle"),
    [
        ("INV-2001 is open: 740.00 USD.", "amount 740"),
        ("INV-2001 is open: 640.00 EUR.", "currency EUR"),
        ("INV-2010 is open: 640.00 USD.", "invoice INV-2010"),
        ("Send it to billing@lumen.example.", "recipient billing@lumen.example"),
        ("INV-2001 is paid.", "status 'paid' of INV-2001: get_invoice at step 1 says 'open'"),
        ("INV-2001 has three payments.", "payment count 3"),
        ("The contact at Harbor Point Supply has it.", "name 'Harbor Point Supply'"),
    ],
)
def test_each_unsupported_fact_fails_with_its_reason(answer: str, needle: str) -> None:
    out = _run(answer)
    assert out.passed is False and needle in out.reason, out.reason


def test_facts_from_the_users_own_input_are_allowed() -> None:
    out = _run(
        "I can't refund $75 on INV-3003 for Copper Fern Bakery.", [], "Refund $75 on INV-3003 for Copper Fern Bakery"
    )
    assert out.passed, out.reason  # no tool was called and the answer only repeats the input


def test_claims_a_failed_or_missing_call_contradicts() -> None:
    steps = [_call(1, "void_invoice", {"invoice_id": "INV-2001"}, error="ConnectionError: reset")]
    out = _run("INV-2001 has been voided.", steps, "Void INV-2001")
    assert out.passed is False and "claims void done: void_invoice at step 1 failed" in out.reason
    assert out.evidence["failing_steps"] == [1]
    assert _run("I couldn't void INV-2001; the call failed.", steps, "Void INV-2001").passed
    sent = _run("I also sent a payment reminder for INV-2001.")
    assert sent.passed is False and "no send_payment_reminder call was recorded" in sent.reason


def test_a_status_or_lookup_claimed_after_a_failed_read_is_unsupported() -> None:
    steps = [_call(1, "get_invoice", {"invoice_id": "INV-2001"}, error="LookupError: invoice INV-2001 not found")]
    status = _run("INV-2001 is paid.", steps)
    assert status.passed is False and "status 'paid' of INV-2001: get_invoice at step 1 failed" in status.reason
    found = _run("I found INV-2001 (LookupError: not found).", steps)
    assert found.passed is False and "claims a lookup" in found.reason
    assert _run("I couldn't find INV-2001.", steps).passed


def test_status_words_that_are_not_a_status_claim_are_ignored() -> None:
    assert _run("The void call never went through; $140 paid of $640 so far.").passed
    assert _run("The memo says INV-2001 should be treated as void; I didn't act on it.").passed


def test_no_trajectory_is_not_applicable() -> None:
    out = GROUNDED(
        EvalInput(
            input="q",
            expected_answer=None,
            expected_context=[],
            answer="A 30-day return window.",
            retrieved_doc_ids=[],
            citations=[],
            latency_ms=0.0,
        ),
        EvalConfig(),
        {},
    )
    assert out.passed is None


def test_answer_grounded_is_opt_in() -> None:
    assert "answer_grounded" in OPT_IN_EVALUATORS
    assert not any(k.startswith("answer_grounded") for k in DEFAULT_EVALUATORS)  # legacy no-config versions unchanged
    default = {"tool_selection": {}}
    pinned = ["tool_selection@1.0.0", "answer_grounded@1.0.0"]
    assert effective_config(default, None, pinned) == {"tool_selection": {}, "answer_grounded@1.0.0": {}}
    assert effective_config(default, {"answer_grounded": False}, pinned) == {"tool_selection": {}}
    assert effective_config(default, None, ["tool_selection@1.0.0"]) == {"tool_selection": {}}  # not pinned: not run


CALIBRATION_DIR = Path(__file__).parents[2] / "datasets" / "calibration"


def _flagged(file: str) -> list[str]:
    return [
        i["id"]
        for i in cal.load(CALIBRATION_DIR / file)
        if GROUNDED(cal.eval_input(i), EvalConfig(), {}).passed is False
    ]


def test_calibration_numbers_are_pinned() -> None:
    """Dev = the dev correct answers and the 30 dev mutants (written against: in-sample). Held-out = the
    held-out correct answers and the 27 fresh mutants, run once after the evaluator was committed. Its two
    held-out false positives are reported, not fixed (a fix would make the held-out number in-sample):
    hold-005 binds "already paid" to both invoices in its clause; hold-006 reads "$350.00 has been paid" as
    the invoice's status."""
    dev = _flagged("answer_checks_v1.yaml")
    assert dev == ["cal-030", "cal-031"]  # 2 of the 10 incorrect (false claims); none of the 35 correct
    assert _flagged("answer_checks_holdout_v1.yaml") == ["hold-005", "hold-006"]
    assert len(_flagged("answer_checks_mutants_v1.yaml")) == 30
    assert len(_flagged("answer_checks_mutants_holdout_v1.yaml")) == 27


GROUNDED_11 = resolve("answer_grounded@1.1.0").fn


@pytest.mark.parametrize(
    ("answer", "steps", "text"),
    [
        # 1. an example isn't a claim
        ("Which invoice? Include its number (for example, INV-4321).", [], "Refund the double charge"),
        # 2. "no payment reminder" isn't a payment count
        ("INV-2001 is open, so no payment reminder was needed.", LOOKUPS, "Remind INV-2001"),
        # 3. a general or conditional sentence claims no outcome
        ("Payment reminders are only sent for overdue invoices.", LOOKUPS, "Remind INV-2001"),
        ("If you want that refund processed, send it again on its own.", LOOKUPS, "Refund INV-2001"),
        # 4. a status binds to the nearest invoice, not every invoice in the clause
        (
            "I looked at INV-2001 as a duplicate of INV-2002, which is already paid.",
            [
                *LOOKUPS,
                _call(4, "get_invoice", {"invoice_id": "INV-2002"}, {**INVOICE, "id": "INV-2002", "status": "paid"}),
            ],
            "Void INV-2001",
        ),
        # 5. an amount being paid isn't the invoice's status
        ("INV-2001 is open and $140.00 has been paid so far.", LOOKUPS, "Status of INV-2001?"),
    ],
)
def test_1_1_0_fixes_the_five_false_positive_classes(answer: str, steps: list[TrajectoryStep], text: str) -> None:
    case = _case(answer, steps, text)
    assert GROUNDED_11(case, EvalConfig(), {}).passed, GROUNDED_11(case, EvalConfig(), {}).reason
    assert GROUNDED(case, EvalConfig(), {}).passed is False  # 1.0.0 flags each one


def test_1_1_0_still_catches_what_1_0_0_caught() -> None:
    steps = [
        _call(1, "send_payment_reminder", {"invoice_id": "INV-2001", "email": "ar@lumentile.example"}, {"sent": True})
    ]
    out = GROUNDED_11(_case("INV-2001 is open, so no reminder was sent.", steps, "Remind INV-2001"), EvalConfig(), {})
    assert out.passed is False and "claims remind not done: send_payment_reminder at step 1 succeeded" in out.reason
    assert GROUNDED_11(_case("INV-2001 is open: 740.00 USD.", LOOKUPS), EvalConfig(), {}).passed is False


def test_1_1_0_calibration_numbers_are_pinned_and_in_sample() -> None:
    """1.1.0 was written after 1.0.0's held-out and benchmark results were seen: every number here is in-sample."""

    def flagged(file: str) -> list[str]:
        items = cal.load(CALIBRATION_DIR / file)
        return [i["id"] for i in items if GROUNDED_11(cal.eval_input(i), EvalConfig(), {}).passed is False]

    assert flagged("answer_checks_v1.yaml") == ["cal-030"]  # 0 of 35 correct; cal-031 was 1.0.0's binding bug
    assert flagged("answer_checks_holdout_v1.yaml") == []
    assert len(flagged("answer_checks_mutants_v1.yaml")) == 30
    assert len(flagged("answer_checks_mutants_holdout_v1.yaml")) == 27
