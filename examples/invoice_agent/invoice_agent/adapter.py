"""AgentForge adapters for the invoice agent.

* `answer` -- this build: the behavior set in `invoice_agent/config.py`. The
  CI release gate evaluates this one, at the base branch and at the PR.
* `answer_v1` / `answer_v2` -- fixed presets (reference, and the regressed
  refactor), independent of config.py, for tests and side-by-side runs.
* `answer_v1_without_d2` -- v1 with only defense D2 off (demo PR #4's config).
* `answer_llm` -- the same agent with an LLM planner (llm_planner.py): OpenAI,
  Anthropic or a local Ollama model chooses the tool calls. Not used by CI
  (which needs no keys); its settings are below.

Each runs the LangGraph agent on one test-case input and reports its
trajectory from the graph's own message history: every tool call (name,
args, and the ToolMessage's result or error) in order, then the final
answer. Trusted local code, executed in-process by the AgentForge worker.

The scripted adapters call no model, so they report no tokens or model name
(None means "not reported"; AgentForge never guesses them). `answer_llm`
reports the token counts its provider returned, summed over the case's model
turns, and the model id it requested. No adapter reports step timings: the
graph doesn't measure them, and AgentForge doesn't invent them.

`scenario` (adversarial cases; agentforge_core.scenario) sets up the case's
environment -- mock tool failures, extra fields in a tool's result, the
session user -- and is applied to the tools and the session only. The
planner never reads it.

Replay overrides (agentforge_sdk.replay): every adapter here accepts the
same settings -- `behavior` (start from the v1 / v2 / v1-without-d2 preset
instead of the adapter's own) and `d1`..`d5` (each defense on or off, applied
on top). Nothing else: the scripted planner has no prompt or model to replace,
and a replay asking for one is rejected. `answer_llm` declares `provider`,
`model`, `temperature`, `prompt` (the base system prompt: `--prompt-file`) and
`d1`..`d5`; a run passes them as its adapter settings, a replay overrides them.

With OpenTelemetry installed, each planner turn and each tool call is a span
(see agent.py / tools.py); each reported step carries the id of its span:
a tool call its `execute_tool` span, the final answer the decision that
produced it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from functools import cache
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

from agentforge_sdk import AdapterOutput, Step
from agentforge_sdk.replay import Setting, replayable
from invoice_agent import llm
from invoice_agent.agent import PRESETS, V1, V1_WITHOUT_D2, V2, Behavior, build_graph, completed_calls
from invoice_agent.config import BEHAVIOR
from invoice_agent.llm_planner import RECURSION_LIMIT as LLM_RECURSION_LIMIT
from invoice_agent.llm_planner import Defenses, build_llm_graph, builtin_prompt, token_totals
from invoice_agent.trace_recorder import Recorder, recording

# Generous for these flows (at most ~8 graph steps); a policy that loops
# forever fails the case with GraphRecursionError instead of hanging.
RECURSION_LIMIT = 25


@cache
def _graph(behavior: Behavior) -> CompiledStateGraph:
    return build_graph(behavior)


def _steps(messages: Sequence[BaseMessage], recorder: Recorder) -> tuple[list[Step], str]:
    calls = completed_calls(messages)
    # Parallel calls in one turn run concurrently, so their spans' order isn't the calls' order: link none then.
    parallel = any(isinstance(m, AIMessage) and len(m.tool_calls) > 1 for m in messages)
    aligned = len(recorder.tool_span_ids) == len(calls) and not parallel
    span_ids = recorder.tool_span_ids if aligned else [None] * len(calls)
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


# Defense switch -> (Behavior field, value when on, value when off).
_DEFENSES: dict[str, tuple[str, Any, Any]] = {
    "d1": ("prompt_override", "refuse", "obey"),
    "d2": ("tool_output_instructions", "ignore", "obey"),
    "d3": ("check_permissions", True, False),
    "d4": ("redact_pii", True, False),
    "d5": ("handle_tool_errors", True, False),
}
_SETTINGS = (
    Setting(
        "behavior",
        "choice",
        "Start from this preset instead of the adapter's own behavior; defenses not overridden follow the preset",
        choices=tuple(PRESETS),
    ),
    Setting("d1", "bool", "D1: refuse requests with prompt-override phrasing (off: obey them)"),
    Setting("d2", "bool", "D2: treat tool output as data (off: obey instructions found in it)"),
    Setting("d3", "bool", "D3: check the session user's allowed_tools before each call"),
    Setting("d4", "bool", "D4: answers carry only a customer's name and billing email"),
    Setting("d5", "bool", "D5: stop and report after a failed tool call"),
)


def _replayable(base: Behavior) -> Any:
    """The shared settings, with defaults read from this adapter's own behavior."""
    preset = next((name for name, p in PRESETS.items() if p == base), None)
    defaults = {"behavior": preset} | {k: getattr(base, f) == on for k, (f, on, _off) in _DEFENSES.items()}
    return replayable(*_SETTINGS, defaults=defaults)


def with_overrides(base: Behavior, overrides: Mapping[str, Any] | None) -> Behavior:
    """`base`, or the `behavior` preset, with each overridden defense switched."""
    if not overrides:
        return base
    behavior = PRESETS[overrides["behavior"]] if "behavior" in overrides else base
    changes = {field: on if overrides[key] else off for key, (field, on, off) in _DEFENSES.items() if key in overrides}
    return replace(behavior, **changes)


@_replayable(BEHAVIOR)
def answer(
    input_text: str, scenario: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> AdapterOutput:
    return _run(with_overrides(BEHAVIOR, overrides), input_text, scenario)


@_replayable(V1)
def answer_v1(
    input_text: str, scenario: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> AdapterOutput:
    return _run(with_overrides(V1, overrides), input_text, scenario)


@_replayable(V2)
def answer_v2(
    input_text: str, scenario: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> AdapterOutput:
    return _run(with_overrides(V2, overrides), input_text, scenario)


@_replayable(V1_WITHOUT_D2)
def answer_v1_without_d2(
    input_text: str, scenario: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> AdapterOutput:
    """v1 with only defense D2 off (demo PR #4's configuration)."""
    return _run(with_overrides(V1_WITHOUT_D2, overrides), input_text, scenario)


# -- the LLM planner ---------------------------------------------------------------

_LLM_SETTINGS = (
    Setting("provider", "choice", "Model provider (key from the worker's environment)", choices=llm.PROVIDERS),
    Setting("model", "str", "Model id, e.g. claude-haiku-4-5, gpt-5.4-mini, llama3.1:8b (Ollama)", max_length=200),
    Setting("temperature", "float", "Sampling temperature (not sent to models that reject it)", minimum=0, maximum=2),
    Setting("prompt", "text", "Base system prompt instead of the built-in one (defense texts are appended)"),
    Setting("d1", "bool", "D1: prompt says to refuse instruction-override requests"),
    Setting("d2", "bool", "D2: prompt says tool output is data, never instructions"),
    Setting("d3", "bool", "D3 (code): offer only the user's permitted tools and block any other call"),
    Setting("d4", "bool", "D4: prompt limits customer details in replies to name and billing email"),
    Setting("d5", "bool", "D5: prompt says to stop and report after a failed tool call"),
)
_LLM_DEFAULTS: dict[str, Any] = {"provider": None, "model": None, "temperature": 0.0, "prompt": None} | {
    f"d{i}": True for i in range(1, 6)
}


def llm_config(settings: Mapping[str, Any]) -> llm.LLMConfig:
    """Provider and model from the settings, else AGENTFORGE_LLM_PROVIDER / AGENTFORGE_LLM_MODEL."""
    provider = settings.get("provider") or os.environ.get("AGENTFORGE_LLM_PROVIDER", "").strip()
    model = settings.get("model") or os.environ.get("AGENTFORGE_LLM_MODEL", "").strip()
    if provider not in llm.PROVIDERS or not model:
        raise llm.LLMError(
            "answer_llm needs a provider and a model: set them as the run's adapter settings "
            "(agentforge compare --models provider:model) or AGENTFORGE_LLM_PROVIDER / AGENTFORGE_LLM_MODEL"
        )
    return llm.LLMConfig(provider=provider, model=model, temperature=float(settings.get("temperature", 0.0)))


@replayable(*_LLM_SETTINGS, defaults=_LLM_DEFAULTS)
def answer_llm(
    input_text: str, scenario: Mapping[str, Any] | None = None, overrides: Mapping[str, Any] | None = None
) -> AdapterOutput:
    """The invoice agent with an LLM planner (see llm_planner.py)."""
    settings = dict(overrides or {})
    config = llm_config(settings)
    defenses = Defenses(**{f"d{i}": bool(settings.get(f"d{i}", True)) for i in range(1, 6)})
    graph = build_llm_graph(config, defenses, settings.get("prompt") or builtin_prompt(), scenario)
    with recording() as recorder:
        state = graph.invoke(
            {"messages": [HumanMessage(content=input_text)]}, config={"recursion_limit": LLM_RECURSION_LIMIT}
        )
    steps, answer = _steps(state["messages"], recorder)
    input_tokens, output_tokens = token_totals(state["messages"])
    return AdapterOutput(
        answer=answer,
        steps=steps,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=config.reported_model,
    )
