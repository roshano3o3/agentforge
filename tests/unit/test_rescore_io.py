"""`agentforge rescore`'s argument parsing and before/after printing, and the worker's version mapping.

The re-score and run dicts here are hand-written in the API's response shape (no API, no worker).
"""

from __future__ import annotations

import re

from rich.console import Console

from agentforge_cli.rescore_io import print_rescore, split_evaluators
from agentforge_worker.rescore import RescorePins

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _metric(name: str, version: str, passed: bool | None) -> dict:
    return {
        "evaluator_name": name,
        "evaluator_version": version,
        "score": None if passed is None else float(passed),
        "value": None,
        "unit": None,
        "passed": passed,
        "reason": "",
        "evidence": {},
        "labels": [],
    }


RUN = {
    "id": "run-1",
    "application_name": "invoice-agent",
    "application_version": "llm",
    "dataset_name": "invoice-agent",
    "dataset_version": 3,
    "labels": [],
    "results": [
        {"id": "res-a", "metrics": [_metric("answer_regex", "1.0.0", False), _metric("tool_selection", "1.0.0", True)]},
        {"id": "res-b", "metrics": [_metric("answer_contains", "1.0.0", True)]},
        {"id": "res-c", "metrics": [_metric("tool_selection", "1.0.0", True)]},
    ],
}


def _result(original_id: str, case: str, before: bool, after: bool, metrics: list[dict]) -> dict:
    return {
        "original_result_id": original_id,
        "case_key": case,
        "status": "ok",
        "original_passed": before,
        "passed": after,
        "changed": before != after,
        "metrics": metrics,
    }


RESCORE = {
    "id": "rs-1",
    "original_run_id": "run-1",
    "status": "completed",
    "replaced": {
        "answer_regex": "answer_regex@1.0.0 -> answer_regex@1.1.0",
        "answer_contains": "answer_contains@1.0.0 -> answer_contains@1.1.0",
    },
    "code_version": "abc1234",
    "error_message": None,
    "newly_passing": 1,
    "newly_failing": 1,
    "original_aggregates": {"pass_rate": 2 / 3},
    "aggregates": {"pass_rate": 2 / 3},
    "results": [
        _result("res-a", "oos-001", False, True, [_metric("answer_regex", "1.1.0", True)]),
        _result("res-b", "void-001", True, False, [_metric("answer_contains", "1.1.0", False)]),
        _result("res-c", "status-001", True, True, [_metric("tool_selection", "1.0.0", True)]),
    ],
}


def _printed(rescore: dict, show_all: bool = False) -> str:
    console = Console(record=True, width=200, force_terminal=False)
    print_rescore(console, rescore, RUN, show_all=show_all)
    return ANSI.sub("", console.export_text())


def test_split_evaluators() -> None:
    assert split_evaluators(["answer_contains@1.1.0,answer_regex", " graceful_tool_failure ", "answer_regex"]) == [
        "answer_contains@1.1.0",
        "answer_regex",
        "graceful_tool_failure",
    ]
    assert split_evaluators([",", ""]) == []


def test_prints_the_changed_cases_before_and_after() -> None:
    out = _printed(RESCORE)
    assert "Re-score rs-1 of run run-1 - completed" in out
    assert "no adapter was called" in out
    assert "answer_regex: answer_regex@1.0.0 -> answer_regex@1.1.0" in out
    oos = next(line for line in out.splitlines() if "oos-001" in line)
    assert re.search(r"FAIL\s.*PASS\s.*fixed\s.*FAIL -> PASS", oos)
    void = next(line for line in out.splitlines() if "void-001" in line)
    assert re.search(r"PASS\s.*FAIL\s.*broken\s.*PASS -> FAIL", void)
    assert "status-001" not in out  # unchanged cases are summarized, not listed
    assert "1 of 3 case(s) unchanged" in out
    assert "pass rate 67% -> 67%" in out and "newly passing 1, newly failing 1" in out


def test_all_lists_unchanged_cases_too() -> None:
    out = _printed(RESCORE, show_all=True)
    assert "status-001" in out and "unchanged" not in out


def test_a_failed_rescore_prints_its_error_and_no_table() -> None:
    failed = {**RESCORE, "status": "failed", "error_message": "worker error: boom", "results": []}
    out = _printed(failed)
    assert "Re-score failed: worker error: boom" in out and "Cases" not in out


def test_a_case_pinning_a_replaced_version_gets_the_new_one() -> None:
    pins = RescorePins(["answer_contains@1.1.0", "tool_selection"], {"answer_contains": "a -> b"})
    found = pins.lookup("answer_contains@1.0.0")
    assert found is not None and found.key == "answer_contains@1.1.0"
    # A name the re-score didn't replace keeps the run's rule: an unpinned exact version is skipped.
    assert pins.lookup("tool_selection@9.9.9") is None
