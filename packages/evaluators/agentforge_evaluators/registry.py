"""The evaluator registry: every evaluator is addressed as `name@version`.

A run records the exact `name@version` of each evaluator it used, and every
stored result records the version that produced it, so changing an
evaluator's behavior means registering a new version -- never editing an
existing one in place, which would silently reinterpret historical results.

Each evaluator also declares the per-case parameters it accepts
(`validate_params`); dataset evaluator configs are checked against these
when the dataset is written, not when a run happens to reach the case.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from agentforge_evaluators import answer_match, operational, retrieval
from agentforge_evaluators.base import EvaluatorFn, EvaluatorKind, EvaluatorSpec, Params, ParamValidator


class UnknownEvaluatorError(ValueError):
    pass


class InvalidParamsError(ValueError):
    pass


_REGISTRY: dict[tuple[str, str], EvaluatorSpec] = {}


def register(
    name: str, version: str, kind: EvaluatorKind, description: str, fn: EvaluatorFn, validate_params: ParamValidator
) -> EvaluatorSpec:
    key = (name, version)
    if key in _REGISTRY:
        raise ValueError(f"evaluator {name}@{version} is already registered")
    spec = EvaluatorSpec(
        name=name, version=version, kind=kind, description=description, fn=fn, validate_params=validate_params
    )
    _REGISTRY[key] = spec
    return spec


def _version_key(version: str) -> tuple[Any, ...]:
    return tuple(int(p) if p.isdigit() else p for p in version.split("."))


def resolve(spec: str) -> EvaluatorSpec:
    """`name@version` -> that exact evaluator; bare `name` -> its highest version."""
    name, _, version = spec.strip().partition("@")
    if version:
        found = _REGISTRY.get((name, version))
        if found is None:
            raise UnknownEvaluatorError(f"unknown evaluator '{spec}'")
        return found
    candidates = [s for (n, _v), s in _REGISTRY.items() if n == name]
    if not candidates:
        raise UnknownEvaluatorError(f"unknown evaluator '{spec}'")
    return max(candidates, key=lambda s: _version_key(s.version))


def list_evaluators() -> list[EvaluatorSpec]:
    return sorted(_REGISTRY.values(), key=lambda s: (s.name, _version_key(s.version)))


# -- parameter validators ------------------------------------------------------


def _params(allowed: Mapping[str, Callable[[Any], Any]], required: frozenset[str] = frozenset()) -> ParamValidator:
    def validate(params: Params) -> dict[str, Any]:
        unknown = sorted(set(params) - set(allowed))
        if unknown:
            accepted = ", ".join(sorted(allowed)) or "none"
            raise InvalidParamsError(f"unknown parameter(s) {unknown} (accepted: {accepted})")
        missing = sorted(required - set(params))
        if missing:
            raise InvalidParamsError(f"missing required parameter(s) {missing}")
        return {key: allowed[key](value) for key, value in params.items()}

    return validate


def _nonblank_strings(value: Any, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise InvalidParamsError("must be a list of non-blank strings")
    if not value and not allow_empty:
        raise InvalidParamsError("must not be empty")
    return list(value)


def _phrases(value: Any) -> list[str]:
    return _nonblank_strings(value, allow_empty=False)


def _doc_ids(value: Any) -> list[str]:
    return _nonblank_strings(value, allow_empty=True)


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise InvalidParamsError("must be a string")
    return value


def _regex(value: Any) -> str:
    pattern = _string(value)
    try:
        re.compile(pattern)
    except re.error as exc:
        raise InvalidParamsError(f"not a valid regular expression: {exc}") from exc
    return pattern


def _positive_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise InvalidParamsError("must be a number > 0")
    return float(value)


_NO_PARAMS = _params({})
_CONTEXT_PARAMS = _params({"expected_context": _doc_ids})


register(
    "exact_match",
    "1.0.0",
    "quality",
    "answer == expected answer after casefold + whitespace normalization (1.0 or 0.0)",
    answer_match.exact_match,
    _params({"expected": _string}),
)
register(
    "answer_contains",
    "1.0.0",
    "quality",
    "fraction of `phrases` found in the answer (case-insensitive); passes only if all found",
    answer_match.answer_contains,
    _params({"phrases": _phrases}, frozenset({"phrases"})),
)
register(
    "answer_regex",
    "1.0.0",
    "quality",
    "re.search(`pattern`, answer) (1.0 or 0.0)",
    answer_match.answer_regex,
    _params({"pattern": _regex}, frozenset({"pattern"})),
)
register(
    "heuristic_context_precision",
    "1.0.0",
    "quality",
    "|retrieved ∩ expected_relevant| / |retrieved| over doc IDs (0.0 when nothing retrieved); not RAGAS",
    retrieval.context_precision,
    _CONTEXT_PARAMS,
)
register(
    "heuristic_context_recall",
    "1.0.0",
    "quality",
    "|retrieved ∩ expected_relevant| / |expected_relevant| over doc IDs; n/a when nothing expected; not RAGAS",
    retrieval.context_recall,
    _CONTEXT_PARAMS,
)
register(
    "citation_correctness",
    "1.0.0",
    "quality",
    "|cited ∩ expected_relevant| / |cited|; citing a never-retrieved doc fails the case",
    retrieval.citation_correctness,
    _CONTEXT_PARAMS,
)
register(
    "latency",
    "1.0.0",
    "measurement",
    "wall-clock adapter call time in ms; passes/fails only when a budget (`max_ms` or the run's) is set",
    operational.latency,
    _params({"max_ms": _positive_number}),
)
register(
    "token_usage",
    "1.0.0",
    "measurement",
    "input + output tokens as reported by the adapter; never estimated",
    operational.token_usage,
    _NO_PARAMS,
)
register(
    "estimated_cost",
    "1.0.0",
    "measurement",
    "ESTIMATED USD: adapter-reported tokens x per-model rates from the pricing config",
    operational.estimated_cost,
    _NO_PARAMS,
)

DEFAULT_EVALUATORS: list[str] = [s.key for s in list_evaluators()]
