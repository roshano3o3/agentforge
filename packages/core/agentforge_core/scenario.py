"""A test case's `scenario`: how the application's environment is set up for
that one case. It is the only part of a case, besides its input text, that
the application under test receives (see agentforge_sdk.adapter).

It describes the *environment*, never the test: no attack category, no
expected outcome. An agent that could read "this is a prompt-injection test"
could pass it by recognizing the test instead of resisting the attack, so
that metadata lives in the case's `safety` block, which only evaluators see.

    tool_overrides:            # per tool name, what the mock tool does instead
      get_customer:
        error: {type: TimeoutError, message: "ledger did not respond in 5s"}
      get_invoice:
        merge_result: {memo: "..."}      # merged into the tool's normal result
    user:                      # the authenticated user for this session
      role: viewer
      allowed_tools: [get_invoice, get_customer]   # absent = no restriction

An adapter implements these semantics for its own mock tools; AgentForge
only validates the shape and delivers the block.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SCENARIO_KEYS = frozenset({"tool_overrides", "user"})
OVERRIDE_KEYS = frozenset({"error", "merge_result"})
USER_KEYS = frozenset({"role", "allowed_tools"})
# Exception types a mock tool may raise for a simulated failure.
TOOL_ERROR_TYPES = frozenset(
    {"TimeoutError", "ConnectionError", "PermissionError", "RuntimeError", "LookupError", "ValueError"}
)


class ScenarioError(ValueError):
    pass


def _nonblank(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioError(f"{where} must be a non-blank string")
    return value


def _override(tool: str, raw: Any) -> dict[str, Any]:
    where = f"tool_overrides.{tool}"
    if not isinstance(raw, Mapping) or not raw:
        raise ScenarioError(f"{where} must be a mapping with 'error' or 'merge_result'")
    unknown = sorted(set(raw) - OVERRIDE_KEYS)
    if unknown:
        raise ScenarioError(f"{where} has unknown key(s) {unknown} (accepted: {sorted(OVERRIDE_KEYS)})")
    if "error" in raw and "merge_result" in raw:
        raise ScenarioError(f"{where}: a tool either fails ('error') or returns ('merge_result'), not both")
    if "error" in raw:
        error = raw["error"]
        if not isinstance(error, Mapping) or set(error) != {"type", "message"}:
            raise ScenarioError(f"{where}.error must be {{type, message}}")
        if error["type"] not in TOOL_ERROR_TYPES:
            raise ScenarioError(f"{where}.error.type must be one of {sorted(TOOL_ERROR_TYPES)}")
        return {"error": {"type": error["type"], "message": _nonblank(f"{where}.error.message", error["message"])}}
    merge = raw["merge_result"]
    if not isinstance(merge, Mapping) or not merge or not all(isinstance(k, str) for k in merge):
        raise ScenarioError(f"{where}.merge_result must be a non-empty mapping")
    return {"merge_result": dict(merge)}


def _user(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ScenarioError("user must be a mapping")
    unknown = sorted(set(raw) - USER_KEYS)
    if unknown:
        raise ScenarioError(f"user has unknown key(s) {unknown} (accepted: {sorted(USER_KEYS)})")
    out: dict[str, Any] = {}
    if "role" in raw:
        out["role"] = _nonblank("user.role", raw["role"])
    if "allowed_tools" in raw:
        tools = raw["allowed_tools"]
        if not isinstance(tools, list) or not all(isinstance(t, str) and t.strip() for t in tools):
            raise ScenarioError("user.allowed_tools must be a list of tool names")
        out["allowed_tools"] = list(tools)
    return out


def validate_scenario(raw: Any) -> dict[str, Any] | None:
    """Validate and normalize a scenario block. None stays None."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ScenarioError("scenario must be a mapping")
    unknown = sorted(set(raw) - SCENARIO_KEYS)
    if unknown:
        raise ScenarioError(f"unknown key(s) {unknown} (accepted: {sorted(SCENARIO_KEYS)})")
    out: dict[str, Any] = {}
    if "tool_overrides" in raw:
        overrides = raw["tool_overrides"]
        if not isinstance(overrides, Mapping) or not overrides:
            raise ScenarioError("tool_overrides must be a non-empty mapping of tool name -> override")
        out["tool_overrides"] = {_nonblank("tool name", t): _override(t, o) for t, o in overrides.items()}
    if "user" in raw:
        out["user"] = _user(raw["user"])
    return out
