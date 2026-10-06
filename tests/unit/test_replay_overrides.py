"""The replay override contract (agentforge_sdk.replay), the example adapters'
declarations, the worker's check, and the CLI's flag parsing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agentforge_cli.replay_io import OverrideArgsError, build_overrides, parse_value
from agentforge_sdk.replay import OverrideError, Setting, replay_settings, replayable, validate_overrides
from agentforge_worker.adapters import PythonAdapter, check_overrides
from invoice_agent import adapter as invoice
from invoice_agent.agent import V1, V1_WITHOUT_D2, V2
from rag_app import adapter as rag

SETTINGS = replayable(
    Setting("flag", "bool"),
    Setting("k", "int", minimum=1, maximum=5),
    Setting("ratio", "float", minimum=0, maximum=1),
    Setting("mode", "choice", choices=("a", "b")),
    Setting("prompt", "text", max_length=10),
    Setting("config", "object", fields=(Setting("depth", "int", minimum=0),)),
)


@SETTINGS
def _adapter(input_text: str, overrides=None):  # noqa: ANN001, ANN202
    return input_text


DECLARED = replay_settings(_adapter)


def _check(overrides: dict) -> dict:
    return validate_overrides(DECLARED, overrides, target="m:f")


def test_valid_overrides_are_normalized() -> None:
    assert _check({}) == {}
    assert _check({"flag": "ON", "k": 5, "ratio": 0, "mode": "b", "prompt": "short", "config": {"depth": 0}}) == {
        "flag": True,
        "k": 5,
        "ratio": 0,
        "mode": "b",
        "prompt": "short",
        "config": {"depth": 0},
    }
    assert _check({"flag": "off"}) == {"flag": False}
    assert _check({"flag": False}) == {"flag": False}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"nope": 1}, "unknown setting 'nope' (accepted: config, flag, k, mode, prompt, ratio)"),
        ({"flag": "maybe"}, "'flag' must be on/off (true/false), got 'maybe'"),
        ({"flag": 1}, "'flag' must be on/off (true/false), got 1"),
        ({"k": "3"}, "'k' must be an integer, got '3'"),
        ({"k": True}, "'k' must be an integer, got True"),
        ({"k": 2.5}, "'k' must be an integer, got 2.5"),
        ({"k": 0}, "'k' must be >= 1, got 0"),
        ({"k": 6}, "'k' must be <= 5, got 6"),
        ({"ratio": float("nan")}, "'ratio' must be a finite number"),
        ({"mode": "c"}, "'mode' must be one of a, b; got 'c'"),
        ({"prompt": "x" * 11}, "'prompt' is 11 characters; at most 10 allowed"),
        ({"prompt": 5}, "'prompt' must be a string, got 5"),
        ({"config": [1]}, "'config' must be an object with keys among depth"),
        ({"config": {"width": 1}}, "unknown setting 'config.width' (accepted: depth)"),
        ({"config": {"depth": -1}}, "'config.depth' must be >= 0, got -1"),
    ],
)
def test_invalid_overrides_are_rejected_with_the_reason(overrides: dict, message: str) -> None:
    with pytest.raises(OverrideError) as exc:
        _check(overrides)
    assert str(exc.value).startswith("adapter 'm:f': ")
    assert message in str(exc.value)


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(OverrideError) as exc:
        _check({"k": 9, "nope": 1})
    assert "'k' must be <= 5" in str(exc.value) and "unknown setting 'nope'" in str(exc.value)


def test_an_adapter_that_declares_nothing_takes_no_overrides() -> None:
    assert validate_overrides(None, {}, target="m:f") == {}
    with pytest.raises(OverrideError, match="declares no replay settings.*got: a, b.*replay it without overrides"):
        validate_overrides(None, {"b": 1, "a": 2}, target="m:f")


def test_declarations_are_checked_when_written() -> None:
    with pytest.raises(TypeError, match="takes no `overrides` argument"):

        @replayable(Setting("x", "bool"))
        def no_param(input_text: str):  # noqa: ANN202
            return input_text

    with pytest.raises(ValueError, match="duplicate"):
        replayable(Setting("x", "bool"), Setting("x", "int"))
    with pytest.raises(ValueError, match="needs choices"):
        Setting("x", "choice")
    with pytest.raises(ValueError, match="needs fields"):
        Setting("x", "object")


def test_the_invoice_agent_declares_presets_and_defenses_only() -> None:
    for fn in (invoice.answer, invoice.answer_v1, invoice.answer_v2, invoice.answer_v1_without_d2):
        declared = replay_settings(fn)
        assert declared is not None
        assert [s["name"] for s in declared.describe()] == ["behavior", "d1", "d2", "d3", "d4", "d5"]
    assert invoice.with_overrides(V1_WITHOUT_D2, None) is V1_WITHOUT_D2
    assert invoice.with_overrides(V1_WITHOUT_D2, {"d2": True}) == V1
    assert invoice.with_overrides(V1, {"behavior": "v2", "d1": True, "d3": True}) == replace(
        V2, prompt_override="refuse", check_permissions=True
    )
    off = invoice.with_overrides(V1, {"d1": False, "d2": False, "d3": False, "d4": False, "d5": False})
    assert (off.prompt_override, off.tool_output_instructions) == ("obey", "obey")
    assert (off.check_permissions, off.redact_pii, off.handle_tool_errors) == (False, False, False)


def test_the_rag_app_declares_retrieval_settings() -> None:
    declared = replay_settings(rag.answer)
    assert declared is not None
    assert validate_overrides(declared, {"retrieval_config": {"top_k": 1, "min_score": 2}}, target="t") == {
        "retrieval_config": {"min_score": 2, "top_k": 1}
    }
    with pytest.raises(OverrideError, match="top_k is set both directly and in retrieval_config"):
        validate_overrides(declared, {"top_k": 1, "retrieval_config": {"top_k": 2}}, target="t")
    # Without overrides: the adapter's own top_k=2. min_score drops documents sharing too few words.
    assert rag.answer("Is shipping free?").retrieved_doc_ids == ["policy-shipping-002", "policy-cancellation-008"]
    assert rag.answer("Is shipping free?", {"top_k": 3}).retrieved_doc_ids == [
        "policy-shipping-002",
        "policy-cancellation-008",
        "policy-international-005",
    ]
    strict = rag.answer("Is shipping free?", {"retrieval_config": {"top_k": 3, "min_score": 2}})
    assert strict.retrieved_doc_ids == ["policy-shipping-002"]


def test_the_worker_checks_overrides_against_the_loaded_adapter() -> None:
    declared = PythonAdapter("invoice_agent.adapter:answer_v1")
    assert check_overrides(declared, {"d2": "off"}, "invoice_agent.adapter:answer_v1") == {"d2": False}
    plain = PythonAdapter("rag_app.fault_injection:answer")
    assert check_overrides(plain, {}, "rag_app.fault_injection:answer") == {}
    with pytest.raises(OverrideError, match="declares no replay settings"):
        check_overrides(plain, {"top_k": 1}, "rag_app.fault_injection:answer")


def test_cli_flags_become_overrides(tmp_path: Path) -> None:
    assert parse_value("3") == 3 and parse_value("true") is True and parse_value("on") == "on"
    assert parse_value('{"a": 1}') == {"a": 1} and parse_value("v1-without-d2") == "v1-without-d2"
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Be careful.\n", encoding="utf-8")
    config = tmp_path / "retrieval.yaml"
    config.write_text("top_k: 3\nmin_score: 2\n", encoding="utf-8")
    assert build_overrides(["d2=on", "behavior=v1"], prompt, config) == {
        "d2": "on",
        "behavior": "v1",
        "prompt": "Be careful.\n",
        "retrieval_config": {"top_k": 3, "min_score": 2},
    }
    with pytest.raises(OverrideArgsError, match="key=value"):
        build_overrides(["d2"], None, None)
    with pytest.raises(OverrideArgsError, match="'prompt' is set more than once"):
        build_overrides(["prompt=x"], prompt, None)
    bad = tmp_path / "list.yaml"
    bad.write_text("- 1\n", encoding="utf-8")
    with pytest.raises(OverrideArgsError, match="must contain a YAML/JSON object"):
        build_overrides([], None, bad)
