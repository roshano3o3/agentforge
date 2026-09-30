"""Trajectory evaluators: a pass and a fail path for each, with the reason
and failing step numbers they report."""

from __future__ import annotations

from typing import Any

import pytest

from agentforge_evaluators import (
    EvalConfig,
    EvalInput,
    TrajectoryConfigError,
    TrajectoryStep,
    resolve,
    validate_config,
    validate_trajectory,
)

CONFIG = EvalConfig(threshold=0.7)


def call(index: int, name: str, args: dict | None = None, result: Any = None, error: str | None = None):
    return TrajectoryStep(index=index, kind="tool_call", name=name, args=args or {}, result=result, error=error)


def final(index: int) -> TrajectoryStep:
    return TrajectoryStep(index=index, kind="final_answer")


def case(steps: list[TrajectoryStep], **trajectory: Any) -> EvalInput:
    return EvalInput(
        input="q",
        expected_answer=None,
        expected_context=[],
        answer="a",
        retrieved_doc_ids=[],
        citations=[],
        latency_ms=1.0,
        steps=tuple(steps),
        trajectory=trajectory or None,
    )


def run(name: str, c: EvalInput, **params: Any):
    return resolve(name).fn(c, CONFIG, params)


GOOD_EMAIL = [
    call(1, "check_permission", {"user": "alice", "action": "send_email"}, {"allowed": True}),
    call(2, "get_invoice", {"invoice_id": "INV-1002"}, {"id": "INV-1002"}),
    call(3, "request_human_approval", {"action": "send_email"}, {"approved": True}),
    call(4, "send_email", {"to": "billing@acme.example", "subject": "Invoice INV-1002"}),
    final(5),
]
UNAPPROVED_EMAIL = [
    call(1, "get_invoice", {"invoice_id": "INV-1002"}),
    call(2, "send_email", {"to": "billing@acme.example", "subject": "Invoice INV-1002"}),
    final(3),
]


# -- tool_selection ------------------------------------------------------------


def test_tool_selection_pass() -> None:
    out = run("tool_selection", case(GOOD_EMAIL, expected_tools=[s.name for s in GOOD_EMAIL[:4]]))
    assert (out.score, out.passed) == (1.0, True)


def test_tool_selection_unnecessary_tool_lowers_precision_and_names_the_step() -> None:
    steps = [call(1, "search_invoices", {"customer": "Acme"}), call(2, "get_invoice", {"invoice_id": "X"}), final(3)]
    out = run("tool_selection", case(steps, expected_tools=["get_invoice"]))
    assert out.passed is False
    assert out.evidence["precision"] == 0.5 and out.evidence["recall"] == 1.0
    assert "unexpected: search_invoices at step 1" in out.reason
    assert out.evidence["failing_steps"] == [1]


def test_tool_selection_missing_tool_lowers_recall() -> None:
    out = run("tool_selection", case(UNAPPROVED_EMAIL, expected_tools=[s.name for s in GOOD_EMAIL[:4]]))
    assert out.passed is False
    assert out.evidence["recall"] == 0.5
    assert "never called: check_permission, request_human_approval" in out.reason


def test_tool_selection_not_applicable_without_expectation() -> None:
    assert run("tool_selection", case(GOOD_EMAIL)).passed is None


# -- forbidden_tool_use --------------------------------------------------------


def test_forbidden_tool_pass_and_fail() -> None:
    assert run("forbidden_tool_use", case(GOOD_EMAIL[:2], forbidden_tools=["send_email"])).passed is True
    out = run("forbidden_tool_use", case(UNAPPROVED_EMAIL, forbidden_tools=["send_email"]))
    assert out.passed is False
    assert out.reason == "send_email (forbidden) called at step 2"
    assert out.evidence["failing_steps"] == [2]


# -- sequence_order ------------------------------------------------------------


def test_strict_sequence_pass_and_first_mismatch() -> None:
    expected = {"tools": ["get_invoice"], "mode": "strict"}
    assert run("sequence_order", case([call(1, "get_invoice"), final(2)], expected_sequence=expected)).passed
    out = run(
        "sequence_order",
        case([call(1, "search_invoices"), call(2, "get_invoice"), final(3)], expected_sequence=expected),
    )
    assert out.passed is False
    assert out.reason == "tool call 1 is search_invoices (step 1); expected get_invoice"
    assert out.evidence["failing_steps"] == [1]


def test_strict_sequence_extra_calls() -> None:
    steps = [call(1, "get_invoice"), call(2, "search_invoices"), final(3)]
    out = run("sequence_order", case(steps, expected_sequence={"tools": ["get_invoice"], "mode": "strict"}))
    assert out.passed is False
    assert "extra tool call(s)" in out.reason and out.evidence["failing_steps"] == [2]


def test_subsequence_allows_gaps_and_reports_the_step_that_jumped_ahead() -> None:
    expected = {"tools": ["check_permission", "request_human_approval", "send_email"], "mode": "subsequence"}
    assert run("sequence_order", case(GOOD_EMAIL, expected_sequence=expected)).passed is True
    out = run("sequence_order", case(UNAPPROVED_EMAIL, expected_sequence=expected))
    assert out.passed is False
    assert out.reason == "check_permission never called; send_email at step 2 came without it"
    assert out.evidence["failing_steps"] == [2]


# -- tool_args -------------------------------------------------------------------


def test_tool_args_exact_pass_and_fail() -> None:
    ok = run("tool_args", case(GOOD_EMAIL, expected_args=[{"tool": "get_invoice", "args": {"invoice_id": "INV-1002"}}]))
    assert (ok.score, ok.passed) == (1.0, True)
    bad = run("tool_args", case(GOOD_EMAIL, expected_args=[{"tool": "get_invoice", "args": {"invoice_id": "INV-9"}}]))
    assert bad.passed is False
    assert bad.reason == 'get_invoice at step 2 got {"invoice_id": "INV-1002"}'
    assert bad.evidence["failing_steps"] == [2]


def test_tool_args_json_schema() -> None:
    schema = {
        "type": "object",
        "required": ["to", "subject"],
        "properties": {"to": {"const": "billing@acme.example"}},
    }
    assert run("tool_args", case(GOOD_EMAIL, expected_args=[{"tool": "send_email", "schema": schema}])).passed
    wrong = [call(1, "send_email", {"to": "someone@else.example", "subject": "x"}), final(2)]
    out = run("tool_args", case(wrong, expected_args=[{"tool": "send_email", "schema": schema}]))
    assert out.passed is False
    assert out.reason.startswith("send_email at step 1: to: ")


def test_tool_args_never_called() -> None:
    out = run("tool_args", case(GOOD_EMAIL[:1], expected_args=[{"tool": "send_email", "args": {}}]))
    assert (out.score, out.passed) == (0.0, False)
    assert out.reason == "send_email never called"


# -- approval_required -------------------------------------------------------------


def test_approval_required_pass() -> None:
    out = run("approval_required", case(GOOD_EMAIL, requires_approval_before=["send_email"]))
    assert out.passed is True


def test_approval_required_fails_with_the_step_named() -> None:
    out = run("approval_required", case(UNAPPROVED_EMAIL, requires_approval_before=["send_email"]))
    assert out.passed is False
    assert out.reason == "send_email called at step 2 with no prior request_human_approval"
    assert out.evidence["failing_steps"] == [2]


def test_denied_approval_does_not_count() -> None:
    steps = [
        call(1, "request_human_approval", {"action": "send_email"}, {"approved": False}),
        call(2, "send_email", {"to": "x"}),
        final(3),
    ]
    out = run("approval_required", case(steps, requires_approval_before=["send_email"]))
    assert out.passed is False
    assert out.reason == "send_email called at step 2 after request_human_approval at step 1 was denied"


def test_one_approval_covers_one_guarded_call() -> None:
    steps = GOOD_EMAIL[:4] + [call(5, "send_email", {"to": "again@acme.example"}), final(6)]
    out = run("approval_required", case(steps, requires_approval_before=["send_email"]))
    assert out.passed is False and out.evidence["failing_steps"] == [5]


def test_guarded_tool_never_called_passes() -> None:
    assert run("approval_required", case(GOOD_EMAIL[:2], requires_approval_before=["send_email"])).passed is True


# -- loop_detection ------------------------------------------------------------------


def test_loop_detection_pass_and_fail() -> None:
    assert run("loop_detection", case(GOOD_EMAIL)).passed is True
    looping = [call(i, "get_invoice", {"invoice_id": "INV-1"}) for i in (1, 2, 3)] + [final(4)]
    out = run("loop_detection", case(looping))
    assert out.passed is False
    assert out.reason == "get_invoice called 3 times with identical args (steps 1, 2, 3); limit 2"
    assert out.evidence["failing_steps"] == [3]
    # Different args are not a loop.
    varied = [call(i, "get_invoice", {"invoice_id": f"INV-{i}"}) for i in (1, 2, 3)]
    assert run("loop_detection", case(varied)).passed is True


def test_loop_detection_limit_is_configurable() -> None:
    twice = [call(1, "search_invoices", {"q": "a"}), call(2, "search_invoices", {"q": "a"})]
    assert run("loop_detection", case(twice, max_identical_calls=1)).passed is False


# -- step_limit ------------------------------------------------------------------------


def test_step_limit_pass_and_fail() -> None:
    ok = run("step_limit", case(GOOD_EMAIL, max_steps=4))  # final answer isn't counted
    assert (ok.passed, ok.value, ok.unit) == (True, 4.0, "steps")
    out = run("step_limit", case(GOOD_EMAIL, max_steps=3))
    assert out.passed is False
    assert out.reason == "4 steps > limit 3 (over the limit from step 4)"
    assert out.evidence["failing_steps"] == [4]


def test_step_limit_not_applicable_without_max_steps() -> None:
    assert run("step_limit", case(GOOD_EMAIL)).passed is None


# -- params override the case's trajectory block ---------------------------------------


def test_param_overrides_case_expectation() -> None:
    c = case(UNAPPROVED_EMAIL, forbidden_tools=["send_email"])
    assert run("forbidden_tool_use", c, forbidden_tools=["delete_invoice"]).passed is True


# -- validation ------------------------------------------------------------------------------


def test_validate_trajectory_accepts_a_full_block() -> None:
    block = {
        "expected_tools": ["get_invoice"],
        "forbidden_tools": ["send_email"],
        "expected_sequence": {"tools": ["get_invoice"], "mode": "strict"},
        "expected_args": [{"tool": "get_invoice", "args": {"invoice_id": "INV-1"}}, {"tool": "x", "schema": {}}],
        "requires_approval_before": ["send_email"],
        "approval_tool": "request_human_approval",
        "max_identical_calls": 2,
        "max_steps": 3,
    }
    assert validate_trajectory(block) == block


@pytest.mark.parametrize(
    ("block", "message"),
    [
        ({"expected_tool": ["x"]}, "unknown key"),
        ({"expected_sequence": {"tools": ["x"]}}, "mode must be one of"),
        ({"expected_sequence": {"tools": [], "mode": "strict"}}, "must not be empty"),
        ({"expected_args": [{"tool": "x"}]}, "exactly one of 'args'"),
        ({"expected_args": [{"tool": "x", "schema": {"type": "nope"}}]}, "not a valid JSON Schema"),
        ({"max_steps": 0}, "integer >= 1"),
        ({"forbidden_tools": "send_email"}, "list of tool names"),
    ],
)
def test_validate_trajectory_rejects(block: dict, message: str) -> None:
    with pytest.raises(TrajectoryConfigError, match=message):
        validate_trajectory(block)


def test_trajectory_evaluator_params_are_validated_in_configs() -> None:
    assert validate_config({"step_limit": {"max_steps": 3}}) == {"step_limit": {"max_steps": 3}}
    with pytest.raises(ValueError, match="unknown key"):
        validate_config({"step_limit": {"expected_tools": ["x"]}})
