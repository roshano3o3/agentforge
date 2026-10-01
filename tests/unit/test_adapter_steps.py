"""The worker's parsing of an adapter's `steps` (the trajectory contract):
what it accepts, and the specific reason it gives for what it rejects."""

from __future__ import annotations

from typing import Any

import pytest

from agentforge_sdk import AdapterOutput, Step
from agentforge_worker.adapters import AdapterOutputError, coerce_output


def test_dict_output_with_steps_is_parsed_in_order() -> None:
    out = coerce_output(
        {
            "answer": "done",
            "steps": [
                {"kind": "retrieval", "name": "retriever", "retrieved_doc_ids": ["doc-1"]},
                {"kind": "tool_call", "name": "get_invoice", "args": {"invoice_id": "INV-1"}, "result": {"ok": True}},
                {"kind": "tool_call", "name": "issue_refund", "args": {}, "error": "LookupError: nope"},
                {"kind": "final_answer", "output": "done", "duration_ms": 3},
            ],
        }
    )
    assert [(s.kind, s.name) for s in out.steps] == [
        ("retrieval", "retriever"),
        ("tool_call", "get_invoice"),
        ("tool_call", "issue_refund"),
        ("final_answer", ""),
    ]
    assert out.steps[0].retrieved_doc_ids == ["doc-1"]
    assert out.steps[1].result == {"ok": True}
    assert out.steps[2].error == "LookupError: nope"
    assert out.steps[3].duration_ms == 3.0


def test_dataclass_output_keeps_its_steps() -> None:
    out = coerce_output(AdapterOutput(answer="a", steps=[Step(kind="tool_call", name="t", args={"x": 1}, result=[1])]))
    assert out.steps == [Step(kind="tool_call", name="t", args={"x": 1}, result=[1])]


def test_no_steps_means_an_empty_trajectory() -> None:
    assert coerce_output({"answer": "a"}).steps == []
    assert coerce_output("plain answer").steps == []


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ("not-a-list", "'steps' must be a list"),
        ([1], r"steps\[0\] must be an object"),
        ([{"kind": "thinking"}], r"steps\[0\]\.kind must be one of"),
        ([{"kind": "tool_call"}], "needs the tool's name"),
        ([{"kind": "tool_call", "name": "t", "args": [1]}], r"steps\[0\]\.args must be an object"),
        ([{"kind": "tool_call", "name": "t", "error": 500}], r"steps\[0\]\.error must be a string"),
        ([{"kind": "tool_call", "name": "t", "result": {1, 2}}], r"steps\[0\]\.result is not JSON-serializable"),
        ([{"kind": "tool_call", "name": "t", "result": float("nan")}], "not JSON-serializable"),
        ([{"kind": "final_answer", "duration_ms": -1}], "duration_ms must be a non-negative number"),
        ([{"kind": "final_answer", "surprise": 1}], r"unknown key\(s\) \['surprise'\]"),
    ],
)
def test_malformed_steps_are_rejected_with_a_specific_reason(steps: Any, message: str) -> None:
    with pytest.raises(AdapterOutputError, match=message):
        coerce_output({"answer": "a", "steps": steps})
