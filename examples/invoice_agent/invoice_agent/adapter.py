"""AgentForge adapters for the invoice agent.

* `answer` -- this build: the behavior set in `invoice_agent/config.py`. The
  CI release gate evaluates this one, at the base branch and at the PR.
* `answer_v1` / `answer_v2` -- fixed presets (reference, and the regressed
  refactor), independent of config.py, for tests and side-by-side runs.

Each runs the LangGraph agent on one test-case input and reports its
trajectory from the graph's own message history: every tool call (name,
args, and the ToolMessage's result or error) in order, then the final
answer. Trusted local code, executed in-process by the AgentForge worker.

No model is called, so no tokens or model name are reported (None means
"not reported"; AgentForge never guesses them), and no step timings: the
graph doesn't measure them, and AgentForge doesn't invent them.

`scenario` (adversarial cases; agentforge_core.scenario) sets up the case's
environment -- mock tool failures, extra fields in a tool's result, the
session user -- and is applied to the tools and the session only. The
planner never reads it.

With OpenTelemetry installed, each planner turn and each tool call is a span
(see agent.py / tools.py); each reported step carries the id of its span:
a tool call its `execute_tool` span, the final answer the decision that
produced it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import cache
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from agentforge_sdk import AdapterOutput, Step
from invoice_agent.agent import V1, V2, Behavior, build_graph, completed_calls
from invoice_agent.config import BEHAVIOR
from invoice_agent.trace_recorder import Recorder, recording

# Generous for these flows (at most ~8 graph steps); a policy that loops
# forever fails the case with GraphRecursionError instead of hanging.
RECURSION_LIMIT = 25


@cache
def _graph(behavior: Behavior) -> CompiledStateGraph:
    return build_graph(behavior)


def _steps(messages: Sequence[BaseMessage], recorder: Recorder) -> tuple[list[Step], str]:
    calls = completed_calls(messages)
    span_ids = recorder.tool_span_ids if len(recorder.tool_span_ids) == len(calls) else [None] * len(calls)
    steps = [
        Step(kind="tool_call", name=c.name, args=c.args, result=c.result, error=c.error, span_id=span_id)
        for c, span_id in zip(calls, span_ids, strict=True)
    ]
    final = messages[-1]
    answer = str(final.content) if isinstance(final, AIMessage) and not final.tool_calls else ""
    steps.append(Step(kind="final_answer", output=answer, span_id=recorder.final_span_id))
    return steps, answer


def _run(behavior: Behavior, input_text: str, scenario: Mapping[str, Any] | None) -> AdapterOutput:
    graph = _graph(behavior) if scenario is None else build_graph(behavior, scenario)
    with recording() as recorder:
        state = graph.invoke(
            {"messages": [HumanMessage(content=input_text)]}, config={"recursion_limit": RECURSION_LIMIT}
        )
    steps, answer = _steps(state["messages"], recorder)
    return AdapterOutput(answer=answer, steps=steps)


def answer(input_text: str, scenario: Mapping[str, Any] | None = None) -> AdapterOutput:
    return _run(BEHAVIOR, input_text, scenario)


def answer_v1(input_text: str, scenario: Mapping[str, Any] | None = None) -> AdapterOutput:
    return _run(V1, input_text, scenario)


def answer_v2(input_text: str, scenario: Mapping[str, Any] | None = None) -> AdapterOutput:
    return _run(V2, input_text, scenario)
