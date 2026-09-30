"""Per-case evaluator configuration.

A dataset version has a `default_evaluators` config, and each test case may
have its own `evaluators` config. Both are mappings from an evaluator
(`name` or `name@version`) to:

    {param: value, ...}   apply it, with these parameters
    {} or true            apply it with no parameters
    false                 do NOT apply it (only meaningful in a case, to drop a default)

A case's effective config is the dataset default with the case's entries
laid over it, matched by evaluator *name* (so `answer_regex@1.0.0` in a
case replaces `answer_regex` from the default). A run applies exactly the
evaluators in each case's effective config -- nothing else.

When a dataset version has no default config at all (versions written
before per-case config existed), the base is "every evaluator the run
pinned", with no params: exactly the Phase 2 behavior.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from agentforge_evaluators.registry import InvalidParamsError, UnknownEvaluatorError, resolve

# Normalized: key -> params dict, or False (disabled).
EvaluatorConfig = dict[str, dict[str, Any] | bool]


class EvaluatorConfigError(ValueError):
    pass


def validate_config(raw: Any, *, allow_disable: bool = True) -> EvaluatorConfig | None:
    """Validate and normalize a config (True -> {}); None stays None (inherit)."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise EvaluatorConfigError("evaluator config must be a mapping of evaluator -> parameters")
    normalized: EvaluatorConfig = {}
    seen_names: set[str] = set()
    for key, value in raw.items():
        if not isinstance(key, str):
            raise EvaluatorConfigError(f"evaluator key must be a string, got {key!r}")
        try:
            spec = resolve(key)
        except UnknownEvaluatorError as exc:
            raise EvaluatorConfigError(str(exc)) from exc
        if spec.name in seen_names:
            raise EvaluatorConfigError(f"evaluator '{spec.name}' is configured more than once")
        seen_names.add(spec.name)
        if value is False:
            if not allow_disable:
                raise EvaluatorConfigError(f"'{key}: false' only makes sense in a test case (to drop a default)")
            normalized[key] = False
            continue
        if value is True or value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise EvaluatorConfigError(f"'{key}': parameters must be a mapping, true, or false")
        try:
            normalized[key] = spec.validate_params(value)
        except InvalidParamsError as exc:
            raise EvaluatorConfigError(f"'{key}': {exc}") from exc
    return normalized


def _name(key: str) -> str:
    return key.partition("@")[0]


def effective_config(
    default: Mapping[str, Any] | None,
    case: Mapping[str, Any] | None,
    run_evaluators: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """The evaluators (key -> params) that apply to one case."""
    base: Mapping[str, Any] = default if default is not None else {key: {} for key in run_evaluators}
    by_name: dict[str, tuple[str, Any]] = {_name(k): (k, v) for k, v in base.items()}
    for key, value in (case or {}).items():
        by_name[_name(key)] = (key, value)
    return {key: dict(value or {}) for key, value in by_name.values() if value is not False}


def referenced_keys(default: Mapping[str, Any] | None, cases: Iterable[Mapping[str, Any] | None]) -> list[str] | None:
    """Every evaluator key a dataset version's configs can apply, or None if
    the version has no default config (so the base is 'all evaluators')."""
    if default is None:
        return None
    keys = [k for k, v in default.items() if v is not False]
    for case in cases:
        keys += [k for k, v in (case or {}).items() if v is not False]
    return list(dict.fromkeys(keys))
