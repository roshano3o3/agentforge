"""The scenario contract (what an adapter may receive), its delivery by the
worker, and the dataset content hash."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentforge_core.hashing import dataset_content_hash
from agentforge_core.scenario import ScenarioError, validate_scenario
from agentforge_worker.adapters import PythonAdapter, invoke

CASE = {"case_key": "c1", "input": "q", "expected_context": [], "tags": ["a"], "trajectory": {"max_steps": 2}}


def test_validate_scenario_accepts_the_documented_shape() -> None:
    raw = {
        "tool_overrides": {
            "get_customer": {"error": {"type": "TimeoutError", "message": "slow"}},
            "get_invoice": {"merge_result": {"memo": "hi"}},
        },
        "user": {"role": "viewer", "allowed_tools": ["get_invoice"]},
    }
    assert validate_scenario(raw) == raw
    assert validate_scenario(None) is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"category": "pii_probe"}, "unknown key"),
        ({"tool_overrides": {"t": {"error": {"type": "SystemExit", "message": "x"}}}}, "error.type must be one of"),
        (
            {"tool_overrides": {"t": {"error": {"type": "TimeoutError", "message": "x"}, "merge_result": {"a": 1}}}},
            "not both",
        ),
        ({"user": {"allowed_tools": "get_invoice"}}, "list of tool names"),
    ],
)
def test_validate_scenario_rejects_with_a_reason(raw: dict, message: str) -> None:
    with pytest.raises(ScenarioError, match=message):
        validate_scenario(raw)


def test_content_hash_ignores_case_order_and_detects_any_change() -> None:
    other = {**CASE, "case_key": "c2"}
    h = dataset_content_hash({"latency": {}}, [CASE, other])
    assert h == dataset_content_hash({"latency": {}}, [other, CASE])
    assert h != dataset_content_hash({"latency": {}}, [{**CASE, "input": "q2"}, other])
    assert h != dataset_content_hash({"latency": {}}, [{**CASE, "safety": {"category": "pii_probe"}}, other])
    assert h != dataset_content_hash(None, [CASE, other])


def test_content_hash_counts_legacy_fields_only_when_set() -> None:
    from_api = {**CASE, "expected_answer_contains": [], "expected_answer_regex": None}
    assert dataset_content_hash(None, [CASE]) == dataset_content_hash(None, [from_api])
    assert dataset_content_hash(None, [CASE]) != dataset_content_hash(
        None, [{**from_api, "expected_answer_regex": "x"}]
    )


# -- delivery by the worker ------------------------------------------------------------

received: list[Any] = []


def takes_scenario(input_text: str, scenario: dict | None = None) -> str:
    received.append(scenario)
    return "ok"


def no_scenario(input_text: str) -> str:
    return "ok"


def test_python_adapter_receives_the_scenario_keyword() -> None:
    received.clear()
    adapter = PythonAdapter(f"{__name__}:takes_scenario")
    outcome = asyncio.run(invoke(adapter, "q", "c1", 5, {"user": {"role": "viewer"}}))
    assert outcome.status == "ok"
    assert received == [{"user": {"role": "viewer"}}]


def test_a_scenario_case_on_an_adapter_without_the_parameter_is_an_error_not_a_silent_run() -> None:
    adapter = PythonAdapter(f"{__name__}:no_scenario")
    outcome = asyncio.run(invoke(adapter, "q", "c1", 5, {"user": {"role": "viewer"}}))
    assert outcome.status == "error"
    assert outcome.error_type == "agentforge_worker.adapters.ScenarioNotSupportedError"
    assert "takes no `scenario` argument" in (outcome.error_message or "")
    # Ordinary cases still work on it.
    assert asyncio.run(invoke(adapter, "q", "c1", 5, None)).status == "ok"
