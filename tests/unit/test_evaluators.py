"""Unit tests for every registered evaluator, its per-case params, the
registry, the per-case config rules, and pricing."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from agentforge_evaluators import (
    DEFAULT_EVALUATORS,
    EvalConfig,
    EvalInput,
    EvaluatorConfigError,
    ModelPrice,
    PricingConfigError,
    UnknownEvaluatorError,
    effective_config,
    list_evaluators,
    parse_pricing,
    referenced_keys,
    resolve,
    validate_config,
)

BASE = EvalInput(
    input="What is the return window?",
    expected_answer=None,
    expected_context=[],
    answer="",
    retrieved_doc_ids=[],
    citations=[],
    latency_ms=12.0,
)
CONFIG = EvalConfig(threshold=0.7)


def case(**overrides: Any) -> EvalInput:
    return dataclasses.replace(BASE, **overrides)


def run(name: str, c: EvalInput, config: EvalConfig = CONFIG, **params: Any):
    return resolve(name).fn(c, config, params)


# -- registry ----------------------------------------------------------------


PHASE2_EVALUATORS = {
    "exact_match",
    "answer_contains",
    "answer_regex",
    "heuristic_context_precision",
    "heuristic_context_recall",
    "citation_correctness",
    "latency",
    "token_usage",
    "estimated_cost",
}
TRAJECTORY = {
    "tool_selection",
    "forbidden_tool_use",
    "sequence_order",
    "tool_args",
    "approval_required",
    "loop_detection",
    "step_limit",
}
SAFETY = {
    "injection_resisted",
    "pii_leak",
    "graceful_tool_failure",
    "unauthorized_action_blocked",
    "tool_args_valid",
}


def test_registry_has_every_required_evaluator_pinned_at_a_version() -> None:
    assert {s.name for s in list_evaluators()} == PHASE2_EVALUATORS | TRAJECTORY | SAFETY
    assert all("@" in key for key in DEFAULT_EVALUATORS)


def test_default_base_is_unchanged_for_versions_without_config() -> None:
    # Versions published before per-case config apply "every default
    # evaluator"; adding trajectory or safety evaluators must not change that set.
    assert {key.partition("@")[0] for key in DEFAULT_EVALUATORS} == PHASE2_EVALUATORS


def test_resolve_bare_name_and_exact_version() -> None:
    assert resolve("exact_match").key == "exact_match@1.0.0"
    assert resolve("exact_match@1.0.0").key == "exact_match@1.0.0"


@pytest.mark.parametrize("spec", ["nope", "exact_match@9.9.9", "@1.0.0"])
def test_resolve_unknown_raises(spec: str) -> None:
    with pytest.raises(UnknownEvaluatorError):
        resolve(spec)


# -- exact_match ---------------------------------------------------------------


def test_exact_match_normalizes_case_and_whitespace() -> None:
    out = run("exact_match", case(expected_answer="45  Days", answer=" 45 days "))
    assert (out.score, out.passed) == (1.0, True)


def test_exact_match_mismatch_fails() -> None:
    out = run("exact_match", case(expected_answer="45 days", answer="45 days."))
    assert (out.score, out.passed) == (0.0, False)  # punctuation is NOT normalized away


def test_exact_match_param_overrides_expected_answer() -> None:
    out = run("exact_match", case(expected_answer="a long reference", answer="yes"), expected="Yes")
    assert out.passed is True


def test_exact_match_not_applicable_without_expected_answer() -> None:
    out = run("exact_match", case(answer="anything"))
    assert (out.score, out.passed) == (None, None)
    assert out.reason.startswith("not applicable")


# -- answer_contains -----------------------------------------------------------


def test_contains_partial_scores_fraction_and_fails() -> None:
    out = run("answer_contains", case(answer="Within 45 DAYS."), phrases=["45 days", "full refund"])
    assert out.score == pytest.approx(0.5)
    assert out.passed is False
    assert out.evidence["missing"] == ["full refund"]


def test_contains_all_found_passes() -> None:
    out = run("answer_contains", case(answer="returns within 45 days"), phrases=["45 days"])
    assert (out.score, out.passed) == (1.0, True)


def test_contains_not_applicable_without_phrases() -> None:
    assert run("answer_contains", case(answer="x")).passed is None


# -- answer_regex --------------------------------------------------------------


def test_regex_match_and_mismatch() -> None:
    hit = run("answer_regex", case(answer="within 45 days of purchase"), pattern=r"\b45 days\b")
    miss = run("answer_regex", case(answer="within 45 days"), pattern=r"^45")
    assert (hit.score, hit.passed, hit.evidence["matched_text"]) == (1.0, True, "45 days")
    assert (miss.score, miss.passed) == (0.0, False)


def test_regex_invalid_pattern_fails_visibly() -> None:
    out = run("answer_regex", case(answer="x"), pattern="(unclosed")
    assert out.passed is False
    assert "not a valid regular expression" in out.reason


# -- context precision / recall ------------------------------------------------


def test_context_precision_uses_threshold() -> None:
    out = run("heuristic_context_precision", case(retrieved_doc_ids=["a", "b"], expected_context=["a"]))
    assert out.score == pytest.approx(0.5)
    assert out.passed is False  # 0.5 < 0.7
    assert run("heuristic_context_precision", case(retrieved_doc_ids=["a"], expected_context=["a"])).passed is True


def test_context_precision_zero_retrieval_is_zero() -> None:
    out = run("heuristic_context_precision", case(retrieved_doc_ids=[], expected_context=["a"]))
    assert (out.score, out.passed) == (0.0, False)
    assert out.evidence["no_documents_retrieved"] is True


def test_expected_context_param_overrides_the_case_field() -> None:
    out = run(
        "heuristic_context_precision",
        case(retrieved_doc_ids=["a", "b"], expected_context=["a"]),
        expected_context=["a", "b"],
    )
    assert out.score == 1.0


def test_context_recall() -> None:
    out = run("heuristic_context_recall", case(retrieved_doc_ids=["a", "x"], expected_context=["a", "b"]))
    assert out.score == pytest.approx(0.5)
    assert out.evidence["missed_doc_ids"] == ["b"]
    full = run("heuristic_context_recall", case(retrieved_doc_ids=["a", "b", "x"], expected_context=["a", "b"]))
    assert (full.score, full.passed) == (1.0, True)


def test_context_recall_not_applicable_when_nothing_expected() -> None:
    out = run("heuristic_context_recall", case(retrieved_doc_ids=["a"], expected_context=[]))
    assert (out.score, out.passed) == (None, None)


# -- citation_correctness ------------------------------------------------------


def test_citations_correct_and_supported_pass() -> None:
    out = run(
        "citation_correctness",
        case(citations=["a"], retrieved_doc_ids=["a", "b"], expected_context=["a"]),
    )
    assert (out.score, out.passed) == (1.0, True)


def test_citation_to_unretrieved_doc_fails_even_if_relevant() -> None:
    out = run(
        "citation_correctness",
        case(citations=["a"], retrieved_doc_ids=["b"], expected_context=["a"]),
    )
    assert out.score == 1.0
    assert out.passed is False
    assert out.evidence["unsupported_citations"] == ["a"]


def test_citing_irrelevant_doc_scores_fraction() -> None:
    out = run(
        "citation_correctness",
        case(citations=["a", "b", "a"], retrieved_doc_ids=["a", "b"], expected_context=["a"]),
    )
    assert out.score == pytest.approx(0.5)  # duplicates collapsed: [a, b]
    assert out.passed is False


def test_no_citations() -> None:
    assert run("citation_correctness", case(expected_context=[])).passed is True
    missing = run("citation_correctness", case(expected_context=["a"]))
    assert (missing.score, missing.passed) == (0.0, False)


# -- latency / tokens / cost ----------------------------------------------------


def test_latency_without_budget_records_value_only() -> None:
    out = run("latency", case(latency_ms=250.0))
    assert (out.value, out.unit, out.score, out.passed) == (250.0, "ms", None, None)


def test_latency_budget_from_run_or_case_param() -> None:
    config = EvalConfig(max_latency_ms=200)
    assert run("latency", case(latency_ms=250.0), config).passed is False
    assert run("latency", case(latency_ms=150.0), config).passed is True
    # A per-case max_ms takes precedence over the run's budget.
    assert run("latency", case(latency_ms=250.0), config, max_ms=300).passed is True


def test_token_usage_reports_only_what_adapter_reported() -> None:
    none = run("token_usage", case())
    assert (none.value, none.reason) == (None, "not reported by adapter")
    both = run("token_usage", case(input_tokens=100, output_tokens=20))
    assert (both.value, both.unit) == (120.0, "tokens")
    partial = run("token_usage", case(input_tokens=100))
    assert partial.evidence["partial"] is True


def test_estimated_cost_is_labeled_estimated_and_uses_configured_rates() -> None:
    config = EvalConfig(pricing={"m": ModelPrice(input_usd_per_million_tokens=3.0, output_usd_per_million_tokens=15.0)})
    out = run("estimated_cost", case(input_tokens=1_000_000, output_tokens=100_000, model="m"), config)
    assert out.value == pytest.approx(3.0 + 1.5)
    assert out.unit == "usd"
    assert out.labels == ["estimated"]


@pytest.mark.parametrize(
    ("overrides", "reason_part"),
    [
        ({}, "no token usage"),
        ({"input_tokens": 5}, "no model name"),
        ({"input_tokens": 5, "model": "unpriced"}, "no entry in the pricing config"),
    ],
)
def test_estimated_cost_never_guesses(overrides: dict, reason_part: str) -> None:
    out = run("estimated_cost", case(**overrides))
    assert out.value is None
    assert reason_part in out.reason
    assert out.labels == ["estimated"]


# -- per-case config ---------------------------------------------------------------


def test_validate_config_normalizes_and_checks_params() -> None:
    config = validate_config(
        {"answer_contains": {"phrases": ["x"]}, "latency": True, "token_usage": None, "answer_regex@1.0.0": False}
    )
    assert config == {
        "answer_contains": {"phrases": ["x"]},
        "latency": {},
        "token_usage": {},
        "answer_regex@1.0.0": False,
    }
    assert validate_config(None) is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"made_up": {}}, "unknown evaluator"),
        ({"answer_contains": {}}, "missing required parameter"),
        ({"answer_contains": {"phrases": []}}, "must not be empty"),
        ({"answer_contains": {"phrases": ["ok"], "typo": 1}}, "unknown parameter"),
        ({"answer_regex": {"pattern": "(unclosed"}}, "not a valid regular expression"),
        ({"latency": {"max_ms": -1}}, "must be a number > 0"),
        ({"token_usage": {"anything": 1}}, "unknown parameter"),
        ({"exact_match": {}, "exact_match@1.0.0": {}}, "configured more than once"),
        (["not", "a", "mapping"], "must be a mapping"),
    ],
)
def test_validate_config_rejects_bad_configs(raw: Any, message: str) -> None:
    with pytest.raises(EvaluatorConfigError, match=message):
        validate_config(raw)


def test_disabling_is_only_allowed_per_case() -> None:
    with pytest.raises(EvaluatorConfigError, match="only makes sense in a test case"):
        validate_config({"latency": False}, allow_disable=False)


def test_effective_config_merges_case_over_default_by_name() -> None:
    default = {"heuristic_context_precision": {}, "latency": {}}
    case_config = {
        "answer_contains@1.0.0": {"phrases": ["x"]},
        "latency": {"max_ms": 50},
        "heuristic_context_precision": False,
    }
    assert effective_config(default, case_config, run_evaluators=[]) == {
        "latency": {"max_ms": 50},
        "answer_contains@1.0.0": {"phrases": ["x"]},
    }
    assert effective_config(default, None, run_evaluators=[]) == default


def test_effective_config_without_default_is_every_run_evaluator() -> None:
    # Versions written before per-case config: the Phase 2 behavior.
    run_evaluators = ["exact_match@1.0.0", "latency@1.0.0"]
    assert effective_config(None, None, run_evaluators) == {"exact_match@1.0.0": {}, "latency@1.0.0": {}}
    assert effective_config(None, {"latency": False}, run_evaluators) == {"exact_match@1.0.0": {}}


def test_referenced_keys() -> None:
    assert referenced_keys(None, [{"latency": {}}]) is None  # base is "all"
    assert referenced_keys({"latency": {}}, [{"answer_regex": {"pattern": "x"}}, None, {"latency": False}]) == [
        "latency",
        "answer_regex",
    ]


# -- pricing config ----------------------------------------------------------------


def test_parse_pricing() -> None:
    prices = parse_pricing({"models": {"m": {"input_usd_per_million_tokens": 1, "output_usd_per_million_tokens": 2}}})
    assert prices == {"m": ModelPrice(1.0, 2.0)}
    assert parse_pricing(None) == {}


@pytest.mark.parametrize(
    "raw",
    [
        ["not", "a", "mapping"],
        {"models": {"m": {"input_usd_per_million_tokens": 1}}},
        {"models": {"m": {"input_usd_per_million_tokens": -1, "output_usd_per_million_tokens": 1}}},
    ],
)
def test_parse_pricing_rejects_bad_config(raw: object) -> None:
    with pytest.raises(PricingConfigError):
        parse_pricing(raw)
