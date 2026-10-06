"""Replay overrides: the settings an adapter accepts when one case is re-run.

AgentForge can replay a single evaluated case -- same input, same scenario,
same dataset version and evaluator versions -- with some of the agent's
settings changed ("would this case pass with defense D2 back on?"). Which
settings can change is up to the adapter, and it has to say so: a python
adapter declares them with `@replayable(...)` and takes an `overrides`
keyword argument.

    from agentforge_sdk.replay import Setting, replayable

    @replayable(
        Setting("top_k", "int", "Documents to retrieve", minimum=1, maximum=10),
        Setting("prompt", "text", "System prompt to use instead of the built-in one"),
    )
    def answer(input_text, overrides=None):
        top_k = (overrides or {}).get("top_k", 2)
        ...

The worker validates a replay's overrides against that declaration *before*
calling the adapter (`validate_overrides`). Anything the adapter didn't
declare -- an unknown name, the wrong type, a value out of range, an option
that isn't one of the choices -- rejects the replay with a message naming
the problem and what is accepted. Nothing is ever silently ignored: an
adapter that declares nothing can only be replayed without overrides, and
`overrides` is passed only when there are some (a replay without overrides
calls the adapter exactly as the original run did).

What a setting *does* is entirely the adapter's business; AgentForge only
checks the declared shape. HTTP adapters can't declare settings (yet), so
they can only be replayed without overrides.

Plain dataclasses, no server/DB imports -- like the rest of the SDK.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

SettingKind = Literal["bool", "int", "float", "str", "choice", "text", "object"]
F = TypeVar("F", bound=Callable[..., Any])

_ATTRIBUTE = "__agentforge_replay__"
_TRUE = {"on", "true", "yes", "1"}
_FALSE = {"off", "false", "no", "0"}


class OverrideError(ValueError):
    """A replay's overrides don't match what the adapter declared."""


@dataclass(frozen=True)
class Setting:
    """One setting an adapter accepts as a replay override.

    * ``bool``: true/false, or the strings on/off, true/false, yes/no.
    * ``int`` / ``float``: a number, within ``minimum``/``maximum`` if set.
    * ``str``: any string up to ``max_length``; ``text``: the same, meant for
      long values such as a prompt (the CLI's ``--prompt-file``).
    * ``choice``: one of ``choices``.
    * ``object``: a mapping whose keys must be among ``fields`` (each a
      Setting, validated the same way), e.g. a retrieval config.
    """

    name: str
    kind: SettingKind
    description: str = ""
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 100_000
    fields: tuple[Setting, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "choice" and not self.choices:
            raise ValueError(f"setting '{self.name}': a choice setting needs choices")
        if self.kind == "object" and not self.fields:
            raise ValueError(f"setting '{self.name}': an object setting needs fields")

    def describe(self) -> dict[str, Any]:
        """A JSON-friendly description (for error messages and listings)."""
        out: dict[str, Any] = {"name": self.name, "kind": self.kind}
        if self.description:
            out["description"] = self.description
        if self.choices:
            out["choices"] = list(self.choices)
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.fields:
            out["fields"] = [f.describe() for f in self.fields]
        return out

    def validate(self, value: Any, where: str) -> Any:
        """The normalized value, or OverrideError naming `where` and what's expected."""
        kind = self.kind
        if kind == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
                return value.strip().lower() in _TRUE
            raise OverrideError(f"'{where}' must be on/off (true/false), got {value!r}")
        if kind in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OverrideError(f"'{where}' must be {'an integer' if kind == 'int' else 'a number'}, got {value!r}")
            if kind == "int" and not isinstance(value, int):
                raise OverrideError(f"'{where}' must be an integer, got {value!r}")
            if not math.isfinite(value):
                raise OverrideError(f"'{where}' must be a finite number, got {value!r}")
            if self.minimum is not None and value < self.minimum:
                raise OverrideError(f"'{where}' must be >= {self.minimum:g}, got {value!r}")
            if self.maximum is not None and value > self.maximum:
                raise OverrideError(f"'{where}' must be <= {self.maximum:g}, got {value!r}")
            return value
        if kind in ("str", "text", "choice"):
            if not isinstance(value, str):
                raise OverrideError(f"'{where}' must be a string, got {value!r}")
            if kind == "choice" and value not in self.choices:
                raise OverrideError(f"'{where}' must be one of {', '.join(self.choices)}; got {value!r}")
            if len(value) > self.max_length:
                raise OverrideError(f"'{where}' is {len(value)} characters; at most {self.max_length} allowed")
            return value
        # object
        if not isinstance(value, Mapping):
            raise OverrideError(f"'{where}' must be an object with keys among {_names(self.fields)}, got {value!r}")
        return _validate_mapping(self.fields, value, prefix=f"{where}.")


@dataclass(frozen=True)
class ReplaySettings:
    """What an adapter declared with `replayable`. `check`, if given, runs
    after every value is valid on its own, for rules across settings (e.g.
    "set top_k once"); it raises OverrideError."""

    settings: tuple[Setting, ...]
    check: Callable[[dict[str, Any]], None] | None = None

    def describe(self) -> list[dict[str, Any]]:
        return [s.describe() for s in self.settings]

    def validate(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        values = _validate_mapping(self.settings, overrides, prefix="")
        if self.check is not None and values:
            self.check(values)
        return values


def _names(settings: tuple[Setting, ...]) -> str:
    return ", ".join(sorted(s.name for s in settings))


def _validate_mapping(settings: tuple[Setting, ...], values: Mapping[str, Any], *, prefix: str) -> dict[str, Any]:
    by_name = {s.name: s for s in settings}
    errors: list[str] = []
    out: dict[str, Any] = {}
    for key in sorted(values, key=str):
        setting = by_name.get(key) if isinstance(key, str) else None
        if setting is None:
            errors.append(f"unknown setting '{prefix}{key}' (accepted: {_names(settings)})")
            continue
        try:
            out[key] = setting.validate(values[key], f"{prefix}{key}")
        except OverrideError as exc:
            errors.append(str(exc))
    if errors:
        raise OverrideError("; ".join(errors))
    return out


def replayable(*settings: Setting, check: Callable[[dict[str, Any]], None] | None = None) -> Callable[[F], F]:
    """Declare the replay settings a python adapter accepts. The adapter must
    take an `overrides` keyword argument; it receives only validated values,
    and only when a replay sets some."""
    names = [s.name for s in settings]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate replay setting names: {names}")
    declared = ReplaySettings(tuple(settings), check)

    def decorate(fn: F) -> F:
        if "overrides" not in inspect.signature(fn).parameters:
            raise TypeError(f"{fn.__qualname__} declares replay settings but takes no `overrides` argument")
        setattr(fn, _ATTRIBUTE, declared)
        return fn

    return decorate


def replay_settings(fn: Any) -> ReplaySettings | None:
    """What `fn` declared with `replayable`, or None."""
    declared = getattr(fn, _ATTRIBUTE, None)
    return declared if isinstance(declared, ReplaySettings) else None


def validate_overrides(declared: ReplaySettings | None, overrides: Mapping[str, Any] | None, *, target: str) -> dict:
    """The validated overrides for a replay of adapter `target`, or
    OverrideError. No overrides is always valid."""
    if not overrides:
        return {}
    if declared is None:
        raise OverrideError(
            f"adapter '{target}' declares no replay settings, so it can't be replayed with overrides "
            f"(got: {', '.join(sorted(map(str, overrides)))}); replay it without overrides"
        )
    try:
        return declared.validate(overrides)
    except OverrideError as exc:
        raise OverrideError(f"adapter '{target}': {exc}") from None
