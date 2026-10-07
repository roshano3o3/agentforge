"""The invoice agent's LLM planner, against fake providers (no network, no keys).

* provider responses (fixtures shaped like each API's documented response,
  loaded through the SDKs' own models) parse into turns with tool calls and
  token counts; unparseable tool arguments are kept verbatim, never repaired;
* the conversation goes back to each provider in its own format, Claude's
  assistant blocks (thinking included) exactly as received;
* the planner runs the same LangGraph agent and tools: trajectory, summed
  tokens and the reported model; D3 is enforced in code, D1/D2/D4/D5 are
  prompt text; a prompt file replaces only the base prompt;
* model, provider, temperature and prompt are declared replay settings (so
  `--set model=...` and `--prompt-file` work); the scripted adapters still
  reject them;
* keys never leak: provider errors are scrubbed and carry no chained cause.
"""

from __future__ import annotations

import json
import traceback
from pathlib import Path

import anthropic
import openai
import pytest
from llm_fakes import PlannerBackedModel, ScriptedTurns, turn, use

from agentforge_sdk.replay import OverrideError, replay_settings, validate_overrides
from invoice_agent import adapter, llm
from invoice_agent.agent import User
from invoice_agent.llm_planner import (
    D1_TEXT,
    D2_TEXT,
    D4_TEXT,
    D5_TEXT,
    MAX_MODEL_TURNS,
    Defenses,
    builtin_prompt,
    system_prompt,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
LLM = {"provider": "anthropic", "model": "claude-haiku-4-5"}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# -- provider responses ---------------------------------------------------------------


def test_openai_response_with_tool_calls_and_usage() -> None:
    response = openai.types.chat.ChatCompletion.model_validate(_fixture("openai_tool_call.json"))
    t = llm.parse_openai(response, temperature_applied=False)
    assert [(c.id, c.name) for c in t.tool_calls] == [
        ("call_fixture_a", "get_invoice"),
        ("call_fixture_b", "get_payment_history"),
    ]
    assert t.tool_calls[0].args == {"invoice_id": "INV-1002"}
    # Invalid JSON from the model is kept as sent; the tool node then rejects the call.
    assert t.tool_calls[1].args == {"__unparsed_arguments__": '{"invoice_id": INV-1002}'}
    assert (t.input_tokens, t.output_tokens) == (812, 57)
    assert t.response_model == "gpt-5.4-mini-2026-03-05" and t.stop_reason == "tool_calls"
    assert [tc["id"] for tc in t.native["tool_calls"]] == ["call_fixture_a", "call_fixture_b"]
    assert t.temperature_applied is False


def test_ollama_final_answer() -> None:
    response = openai.types.chat.ChatCompletion.model_validate(_fixture("ollama_final_answer.json"))
    t = llm.parse_openai(response, temperature_applied=True)
    assert t.tool_calls == [] and t.text.startswith("INV-1002 is open")
    assert (t.input_tokens, t.output_tokens) == (690, 21)


def test_anthropic_response_keeps_every_block_for_the_next_turn() -> None:
    response = anthropic.types.Message.model_validate(_fixture("anthropic_tool_use.json"))
    t = llm.parse_anthropic(response, temperature_applied=True)
    assert [(c.id, c.name, c.args) for c in t.tool_calls] == [
        ("toolu_fixture_01", "get_invoice", {"invoice_id": "INV-1002"})
    ]
    assert t.text == "Let me look that invoice up."
    assert (t.input_tokens, t.output_tokens) == (1034, 88)
    assert [b["type"] for b in t.native] == ["thinking", "text", "tool_use"]
    assert t.native[0]["signature"] == "fixture-signature-not-real"


def test_conversation_in_each_providers_format() -> None:
    history = [
        llm.Msg(role="user", text="Status of INV-1002 and its payments?"),
        llm.Msg(role="assistant", native=[{"type": "tool_use", "id": "t1", "name": "get_invoice", "input": {}}]),
        llm.Msg(role="tool", text='{"id": "INV-1002"}', tool_call_id="t1"),
        llm.Msg(role="tool", text="LookupError: nope", tool_call_id="t2", is_error=True),
    ]
    claude = llm.anthropic_messages(history)
    assert [m["role"] for m in claude] == ["user", "assistant", "user"]
    assert claude[1]["content"] == history[1].native  # sent back exactly as received
    assert [(b["tool_use_id"], b["is_error"]) for b in claude[2]["content"]] == [("t1", False), ("t2", True)]

    oa = llm.openai_messages(
        "SYSTEM", [history[0], llm.Msg(role="assistant", native={"role": "assistant"}), history[2]]
    )
    assert [m["role"] for m in oa] == ["system", "user", "assistant", "tool"]
    assert oa[0]["content"] == "SYSTEM" and oa[3] == {
        "role": "tool",
        "tool_call_id": "t1",
        "content": '{"id": "INV-1002"}',
    }


@pytest.mark.parametrize(
    ("provider", "model", "sent"),
    [
        ("anthropic", "claude-haiku-4-5", True),
        ("anthropic", "claude-opus-5-5", False),
        ("anthropic", "claude-sonnet-5-5", False),
        ("openai", "gpt-4.1-mini", True),
        ("openai", "gpt-5.4-mini", False),
        ("ollama", "llama3.1:8b", True),
    ],
)
def test_temperature_is_only_sent_where_the_model_takes_one(provider: str, model: str, sent: bool) -> None:
    assert llm.accepts_temperature(provider, model) is sent


# -- secrets ----------------------------------------------------------------------------


def test_a_missing_key_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENAI_API_KEY is not set in the worker's environment"):
        llm.OpenAIProvider("openai").complete(llm.LLMConfig("openai", "gpt-5.4-mini"), "s", [], [])


def test_provider_errors_never_carry_the_key(monkeypatch) -> None:
    key = "sk-proj-THISISAFAKEKEYFORTESTS1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", key)

    class Exploding:
        def __init__(self, **kwargs) -> None:
            assert kwargs["api_key"] == key  # the key goes to the SDK client, nowhere else
            self.chat = self
            self.completions = self

        def create(self, **kwargs):
            raise RuntimeError(f"401 Incorrect API key provided: {key}. Also seen: sk-ant-api03-ANOTHERFAKEKEY99")

    monkeypatch.setattr(openai, "OpenAI", Exploding)
    with pytest.raises(llm.LLMError) as caught:
        llm.OpenAIProvider("openai").complete(llm.LLMConfig("openai", "gpt-5.4-mini"), "s", [], [])
    rendered = "".join(traceback.format_exception(caught.value))
    assert key not in rendered and "ANOTHERFAKEKEY" not in rendered
    assert "[REDACTED]" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


# -- the planner --------------------------------------------------------------------------


def test_a_model_drives_the_same_graph_and_tools(monkeypatch) -> None:
    fake = use(
        monkeypatch,
        ScriptedTurns(
            [
                turn(calls=[("get_invoice", {"invoice_id": "INV-1002"})], turn_no=1, tokens=(500, 40)),
                turn(calls=[("get_customer", {"customer_id": "CUST-02"})], turn_no=2, tokens=(620, 35)),
                turn(text="The billing contact for INV-1002 is Kestrel Robotics.", turn_no=3, tokens=(700, 25)),
            ]
        ),
    )
    out = adapter.answer_llm("Who is the billing contact for INV-1002?", overrides=LLM)
    assert [(s.kind, s.name) for s in out.steps] == [
        ("tool_call", "get_invoice"),
        ("tool_call", "get_customer"),
        ("final_answer", ""),
    ]
    assert out.steps[0].result["customer_id"] == "CUST-02"  # the real mock tool ran
    assert out.answer == "The billing contact for INV-1002 is Kestrel Robotics."
    assert (out.input_tokens, out.output_tokens) == (500 + 620 + 700, 40 + 35 + 25)
    assert out.model == "claude-haiku-4-5"
    # The model read the tool's full structured result, as JSON, on the next turn.
    tool_msg = fake.calls[1]["messages"][2]
    assert tool_msg.role == "tool" and json.loads(tool_msg.text)["customer_id"] == "CUST-02"
    assert fake.calls[0]["config"] == llm.LLMConfig("anthropic", "claude-haiku-4-5", 0.0)
    assert len(fake.calls[0]["tools"]) == 8


def test_the_planner_backed_stand_in_reproduces_v1_on_a_case(monkeypatch) -> None:
    use(monkeypatch, PlannerBackedModel())
    out = adapter.answer_llm("Refund $40 on INV-1001 - the customer was double charged.", overrides=LLM)
    assert [s.name for s in out.steps] == ["get_invoice", "request_human_approval", "issue_refund", ""]
    assert out.answer.startswith("Refunded 40.00 USD on INV-1001")


def test_d3_offers_only_permitted_tools_and_blocks_anything_else(monkeypatch) -> None:
    scenario = {"user": {"role": "viewer", "allowed_tools": ["get_invoice"]}}
    fake = use(
        monkeypatch,
        ScriptedTurns([turn(calls=[("delete_invoice", {"invoice_id": "INV-1004"})]), turn(text="Done.", turn_no=2)]),
    )
    out = adapter.answer_llm("Delete INV-1004", scenario=scenario, overrides=LLM)
    assert fake.calls[0]["tools"] == ["get_invoice"]
    assert "role is viewer" in fake.calls[0]["system"]
    blocked = out.steps[0]
    # The attempt is reported (the model made it); the tool never ran.
    assert blocked.name == "delete_invoice" and blocked.result is None
    assert (
        blocked.error == "PermissionError: blocked by the permission check (D3): role viewer may not use delete_invoice"
    )

    fake = use(
        monkeypatch, ScriptedTurns([turn(calls=[("delete_invoice", {"invoice_id": "INV-1004"})]), turn(text="ok")])
    )
    out = adapter.answer_llm("Delete INV-1004", scenario=scenario, overrides={**LLM, "d3": False})
    assert len(fake.calls[0]["tools"]) == 8 and "role is viewer" not in fake.calls[0]["system"]
    assert out.steps[0].result == {"invoice_id": "INV-1004", "deleted": True}


def test_defenses_are_prompt_text_and_a_prompt_file_replaces_only_the_base() -> None:
    every = system_prompt("BASE", Defenses(), User())
    assert every.startswith("BASE") and all(t in every for t in (D1_TEXT, D2_TEXT, D4_TEXT, D5_TEXT))
    none = system_prompt("BASE", Defenses(d1=False, d2=False, d3=False, d4=False, d5=False), User())
    assert none == "BASE"
    assert D2_TEXT not in system_prompt("BASE", Defenses(d2=False), User())
    assert "OVR-7731-QX" in builtin_prompt()  # the confidential value the secret-reveal attacks go after


def test_prompt_setting_reaches_the_model(monkeypatch) -> None:
    fake = use(monkeypatch, ScriptedTurns([turn(text="Hello.")]))
    adapter.answer_llm("hi", overrides={**LLM, "prompt": "You are a terse billing bot.", "temperature": 0.7})
    assert fake.calls[0]["system"].startswith("You are a terse billing bot.")
    assert "OVR-7731-QX" not in fake.calls[0]["system"]
    assert fake.calls[0]["config"].temperature == 0.7


def test_a_model_that_never_stops_is_cut_off(monkeypatch) -> None:
    looping = [turn(calls=[("get_invoice", {"invoice_id": "INV-1001"})], turn_no=n) for n in range(1, 40)]
    fake = use(monkeypatch, ScriptedTurns(looping))
    out = adapter.answer_llm("Status of INV-1001?", overrides=LLM)
    assert len(fake.calls) == MAX_MODEL_TURNS
    assert out.answer == f"Stopped: the model was still calling tools after {MAX_MODEL_TURNS} turns."
    assert sum(1 for s in out.steps if s.kind == "tool_call") == MAX_MODEL_TURNS


def test_repeated_tool_call_ids_get_fresh_ones(monkeypatch) -> None:
    first = turn(calls=[("get_invoice", {"invoice_id": "INV-1002"})])
    second = turn(calls=[("get_customer", {"customer_id": "CUST-02"})])
    second.tool_calls[0].id = second.native["tool_calls"][0]["id"] = first.tool_calls[0].id  # a server reusing ids
    use(monkeypatch, ScriptedTurns([first, second, turn(text="done", turn_no=3)]))
    out = adapter.answer_llm("Who is the billing contact for INV-1002?", overrides=LLM)
    assert out.steps[0].result["id"] == "INV-1002" and out.steps[1].result["id"] == "CUST-02"


def test_no_model_configured(monkeypatch) -> None:
    monkeypatch.delenv("AGENTFORGE_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("AGENTFORGE_LLM_MODEL", raising=False)
    with pytest.raises(llm.LLMError, match="needs a provider and a model"):
        adapter.answer_llm("hi")
    monkeypatch.setenv("AGENTFORGE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AGENTFORGE_LLM_MODEL", "llama3.1:8b")
    assert adapter.llm_config({}) == llm.LLMConfig("ollama", "llama3.1:8b", 0.0)
    assert adapter.llm_config({}).reported_model == "ollama/llama3.1:8b"


# -- replay settings ----------------------------------------------------------------------


def test_model_prompt_and_temperature_are_replay_settings() -> None:
    declared = replay_settings(adapter.answer_llm)
    assert declared is not None
    target = "invoice_agent.adapter:answer_llm"
    assert validate_overrides(declared, {"model": "gpt-5.4-mini", "prompt": "p", "temperature": 0.5}, target=target)
    with pytest.raises(OverrideError, match="'provider' must be one of openai, anthropic, ollama"):
        validate_overrides(declared, {"provider": "bedrock"}, target=target)
    with pytest.raises(OverrideError, match="'temperature' must be <= 2"):
        validate_overrides(declared, {"temperature": 3}, target=target)
    # The scripted planner still has no prompt or model to replace.
    with pytest.raises(OverrideError, match="unknown setting 'model'"):
        validate_overrides(replay_settings(adapter.answer_v1), {"model": "x"}, target="invoice_agent.adapter:answer_v1")
