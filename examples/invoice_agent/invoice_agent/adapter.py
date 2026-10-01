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
"""

from __future__ import annotations

from collections.abc import Sequence
from functools import cache

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from agentforge_sdk import AdapterOutput, Step
from invoice_agent.agent import V1, V2, Behavior, build_graph, completed_calls
from invoice_agent.config import BEHAVIOR

# Generous for these flows (at most ~8 graph steps); a policy that loops
# forever fails the case with GraphRecursionError instead of hanging.
RECURSION_LIMIT = 25


@cache
def _graph(behavior: Behavior) -> CompiledStateGraph:
    return build_graph(behavior)


def _steps(messages: Sequence[BaseMessage]) -> tuple[list[Step], str]:
    steps = [
        Step(kind="tool_call", name=c.name, args=c.args, result=c.result, error=c.error)
        for c in completed_calls(messages)
    ]
    final = messages[-1]
    answer = str(final.content) if isinstance(final, AIMessage) and not final.tool_calls else ""
    steps.append(Step(kind="final_answer", output=answer))
    return steps, answer


def _run(behavior: Behavior, input_text: str) -> AdapterOutput:
    state = _graph(behavior).invoke(
        {"messages": [HumanMessage(content=input_text)]}, config={"recursion_limit": RECURSION_LIMIT}
    )
    steps, answer = _steps(state["messages"])
    return AdapterOutput(answer=answer, steps=steps)


def answer(input_text: str) -> AdapterOutput:
    return _run(BEHAVIOR, input_text)


def answer_v1(input_text: str) -> AdapterOutput:
    return _run(V1, input_text)


def answer_v2(input_text: str) -> AdapterOutput:
    return _run(V2, input_text)
