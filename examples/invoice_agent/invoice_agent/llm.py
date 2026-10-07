"""Model providers for the invoice agent's LLM planner (`planner="llm"`).

One small interface over three backends, each through its official SDK:

* `openai`    -- OpenAI's Chat Completions API (`openai` package), key OPENAI_API_KEY;
* `anthropic` -- Claude's Messages API (`anthropic` package), key ANTHROPIC_API_KEY;
* `ollama`    -- a local Ollama server through its OpenAI-compatible endpoint
                 (`openai` package with a base URL), OLLAMA_BASE_URL, no key.

A provider takes the conversation so far in a neutral form (`Msg`) plus the
tool schemas and returns one `Turn`: the model's text, its tool calls, the
token counts the provider reported, and the provider-native assistant message
to send back on the next turn unchanged (Claude's thinking blocks must be
echoed exactly; nothing here edits history).

Keys come only from the environment (the worker container gets them from the
git-ignored .env through docker-compose). They're never put in a span, a log
line, a result or an error message: every provider error is re-raised as
`LLMError` with anything key-shaped -- and the configured keys themselves --
replaced by [REDACTED].

What's *not* sent: sampling parameters a model rejects. Claude Opus 5.5 /
Sonnet 5.5 (and the other 4.7+ / 5.x models) and OpenAI's GPT-5 reasoning
models don't accept `temperature`; for those the setting is recorded as not
applied (`Turn.temperature_applied`), never silently claimed.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ProviderName = Literal["openai", "anthropic", "ollama"]
PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "ollama")
KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434/v1"
# Per model turn. Generous so adaptive thinking isn't cut off; output tokens are what's billed.
MAX_OUTPUT_TOKENS = 16_000
REQUEST_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    model: str
    temperature: float = 0.0

    @property
    def reported_model(self) -> str:
        """The `model` the adapter reports (and the pricing table is keyed by).
        Ollama names are local and could collide with a hosted id, so they're prefixed."""
        return f"ollama/{self.model}" if self.provider == "ollama" else self.model


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Msg:
    """One conversation entry, provider-neutral. `native` is an assistant
    message exactly as its provider returned it (sent back as is)."""

    role: Literal["user", "assistant", "tool"]
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    native: Any = None
    # tool results
    tool_call_id: str = ""
    is_error: bool = False


@dataclass
class Turn:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int | None
    output_tokens: int | None
    response_model: str | None
    stop_reason: str | None
    native: Any
    temperature_applied: bool


class LLMError(RuntimeError):
    """A provider call failed. The message is scrubbed of keys."""


class Provider(Protocol):
    def complete(
        self, config: LLMConfig, system: str, messages: Sequence[Msg], tools: Sequence[Mapping[str, Any]]
    ) -> Turn: ...


# -- secrets -----------------------------------------------------------------------

_KEY_SHAPED = re.compile(r"\b(?:sk-ant-[A-Za-z0-9_-]{8,}|sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{16,})")


def scrub(text: str) -> str:
    """`text` with the configured keys and anything key-shaped replaced."""
    for env in KEY_ENV.values():
        key = os.environ.get(env, "")
        if len(key) >= 8:
            text = text.replace(key, "[REDACTED]")
    return _KEY_SHAPED.sub("[REDACTED]", text)


def _key(provider: str) -> str:
    env = KEY_ENV[provider]
    key = os.environ.get(env, "").strip()
    if not key:
        raise LLMError(f"{env} is not set in the worker's environment (add it to .env; see .env.example)")
    return key


def _failed(provider: str, exc: Exception) -> LLMError:
    return LLMError(scrub(f"{provider} request failed: {type(exc).__name__}: {exc}"))


# -- sampling support ----------------------------------------------------------------


def accepts_temperature(provider: str, model: str) -> bool:
    """Whether a `temperature` is sent. Conservative: only models known to take one."""
    if provider == "ollama":
        return True
    if provider == "anthropic":
        # The 4.7+ / 5.x Claude models reject sampling parameters; Haiku 4.5 and the 4.6 models take them.
        return model.startswith(("claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-6", "claude-sonnet-4-5"))
    # OpenAI: the GPT-5 family and o-series are reasoning models (no temperature); GPT-4.x takes it.
    return not model.startswith(("gpt-5", "o1", "o3", "o4"))


def _args(raw: str | None) -> dict[str, Any]:
    """Tool-call arguments as the model sent them. Unparseable JSON is kept verbatim (the call then
    fails validation in the tool node and is recorded with that error), never repaired."""
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__unparsed_arguments__": raw}
    return value if isinstance(value, dict) else {"__unparsed_arguments__": raw}


# -- OpenAI / Ollama -----------------------------------------------------------------


def openai_messages(system: str, messages: Sequence[Msg]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        elif m.role == "assistant":
            out.append(m.native)
        else:
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.text})
    return out


def openai_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
        }
        for t in tools
    ]


def parse_openai(response: Any, temperature_applied: bool) -> Turn:
    """A Turn from a ChatCompletion (the SDK object, or one validated from recorded JSON)."""
    choice = response.choices[0]
    message = choice.message
    calls = [
        ToolCall(id=tc.id, name=tc.function.name, args=_args(tc.function.arguments))
        for tc in (message.tool_calls or [])
        if getattr(tc, "function", None) is not None
    ]
    native: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        native["tool_calls"] = [tc.model_dump(exclude_none=True) for tc in message.tool_calls]
    usage = response.usage
    return Turn(
        text=message.content or (message.refusal or ""),
        tool_calls=calls,
        input_tokens=usage.prompt_tokens if usage else None,
        output_tokens=usage.completion_tokens if usage else None,
        response_model=response.model,
        stop_reason=choice.finish_reason,
        native=native,
        temperature_applied=temperature_applied,
    )


class OpenAIProvider:
    def __init__(self, provider: str = "openai") -> None:
        self.provider = provider

    def _client(self) -> Any:
        import openai

        if self.provider == "ollama":
            base_url = os.environ.get("OLLAMA_BASE_URL", "").strip() or DEFAULT_OLLAMA_BASE_URL
            return openai.OpenAI(api_key="ollama", base_url=base_url, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=1)
        return openai.OpenAI(api_key=_key("openai"), timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2)

    def complete(
        self, config: LLMConfig, system: str, messages: Sequence[Msg], tools: Sequence[Mapping[str, Any]]
    ) -> Turn:
        temperature = accepts_temperature(self.provider, config.model)
        kwargs: dict[str, Any] = {"model": config.model, "messages": openai_messages(system, messages)}
        if tools:
            kwargs["tools"] = openai_tools(tools)
        if temperature:
            kwargs["temperature"] = config.temperature
        try:
            response = self._client().chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001 - every SDK error leaves here scrubbed
            raise _failed(self.provider, exc) from None
        return parse_openai(response, temperature)


# -- Anthropic -----------------------------------------------------------------------


def anthropic_messages(messages: Sequence[Msg]) -> list[dict[str, Any]]:
    """Claude's message list: tool results of one assistant turn go back in a single user message."""
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            pending.append(
                {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.text, "is_error": m.is_error}
            )
            continue
        if pending:
            out.append({"role": "user", "content": pending})
            pending = []
        if m.role == "user":
            out.append({"role": "user", "content": m.text})
        else:
            out.append({"role": "assistant", "content": m.native})
    if pending:
        out.append({"role": "user", "content": pending})
    return out


def anthropic_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]


def parse_anthropic(response: Any, temperature_applied: bool) -> Turn:
    """A Turn from a Message (the SDK object, or one validated from recorded JSON)."""
    text = "".join(b.text for b in response.content if b.type == "text")
    calls = [ToolCall(id=b.id, name=b.name, args=dict(b.input)) for b in response.content if b.type == "tool_use"]
    return Turn(
        text=text,
        tool_calls=calls,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        response_model=response.model,
        stop_reason=response.stop_reason,
        # Every block, thinking included, goes back exactly as received.
        native=[b.to_dict() for b in response.content],
        temperature_applied=temperature_applied,
    )


class AnthropicProvider:
    provider = "anthropic"

    def complete(
        self, config: LLMConfig, system: str, messages: Sequence[Msg], tools: Sequence[Mapping[str, Any]]
    ) -> Turn:
        import anthropic

        temperature = accepts_temperature("anthropic", config.model)
        kwargs: dict[str, Any] = {
            "model": config.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "system": system,
            "messages": anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = anthropic_tools(tools)
        if temperature:
            # Not a typed parameter in anthropic 1.x (newer models reject it); sent only where accepted.
            kwargs["extra_body"] = {"temperature": config.temperature}
        try:
            client = anthropic.Anthropic(api_key=_key("anthropic"), timeout=REQUEST_TIMEOUT_SECONDS, max_retries=2)
            response = client.messages.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _failed("anthropic", exc) from None
        return parse_anthropic(response, temperature)


def provider_for(name: str) -> Provider:
    """The provider for a name. Tests replace this module attribute with a fake."""
    if name == "anthropic":
        return AnthropicProvider()
    if name in ("openai", "ollama"):
        return OpenAIProvider(name)
    raise LLMError(f"unknown provider '{name}' (one of: {', '.join(PROVIDERS)})")
