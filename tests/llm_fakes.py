"""Fake model providers for the invoice agent's LLM planner (no network, no keys).

* `ScriptedTurns` replays a fixed list of turns, one per model call, and
  records what the planner sent (system prompt, message roles, offered tools).
* `PlannerBackedModel` is a stand-in "model" whose every decision is the
  scripted v1 planner's (`invoice_agent.agent.plan`) on the conversation so
  far, read back from what the LLM planner sent it: the tool calls in its own
  earlier turns and the JSON tool results. It exercises the whole LLM path
  (message conversion, tool results as JSON, call/result pairing, token sums)
  end to end. It's not a model, and its numbers say nothing about one.

Token counts are made up per turn by the fakes (fixed small numbers) so cost
arithmetic has something to multiply; they're test inputs, not measurements.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from invoice_agent import llm
from invoice_agent.agent import V1, Call, Next, plan


def turn(text: str = "", calls: Sequence[tuple[str, dict]] = (), *, turn_no: int = 1, tokens=(100, 20)) -> llm.Turn:
    tool_calls = [llm.ToolCall(id=f"call_{turn_no}_{n}", name=name, args=args) for n, (name, args) in enumerate(calls)]
    native = {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
            for c in tool_calls
        ],
    }
    return llm.Turn(
        text=text,
        tool_calls=tool_calls,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        response_model="fake-model",
        stop_reason="tool_calls" if tool_calls else "stop",
        native=native,
        temperature_applied=True,
    )


class ScriptedTurns:
    def __init__(self, turns: Sequence[llm.Turn]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def complete(self, config, system, messages, tools) -> llm.Turn:
        self.calls.append(
            {
                "config": config,
                "system": system,
                "roles": [m.role for m in messages],
                "tools": [t["name"] for t in tools],
                "messages": list(messages),
            }
        )
        return self.turns.pop(0)


class PlannerBackedModel:
    """See the module docstring. `calls` counts model calls; `systems` keeps each system prompt."""

    def __init__(self, input_tokens: int = 120, output_tokens: int = 30) -> None:
        self.tokens = (input_tokens, output_tokens)
        self.calls = 0
        self.systems: list[str] = []

    def complete(self, config, system, messages: Sequence[llm.Msg], tools: Sequence[Mapping[str, Any]]) -> llm.Turn:
        self.calls += 1
        self.systems.append(system)
        request = next(m.text for m in messages if m.role == "user")
        results = {m.tool_call_id: m for m in messages if m.role == "tool"}
        done: list[Call] = []
        for m in messages:
            if m.role != "assistant":
                continue
            for tc in m.native.get("tool_calls", []):
                res = results[tc["id"]]
                args = json.loads(tc["function"]["arguments"])
                if res.is_error:
                    done.append(Call(tc["function"]["name"], args, None, res.text))
                else:
                    done.append(Call(tc["function"]["name"], args, json.loads(res.text), None))
        action = plan(V1, request, done)
        turn_no = 1 + sum(1 for m in messages if m.role == "assistant")
        if isinstance(action, Next):
            return turn(calls=[(action.tool, action.args)], turn_no=turn_no, tokens=self.tokens)
        return turn(text=action, turn_no=turn_no, tokens=self.tokens)


def use(monkeypatch, provider: Any) -> Any:
    """Make every provider lookup return `provider`."""
    monkeypatch.setattr(llm, "provider_for", lambda name: provider)
    return provider
