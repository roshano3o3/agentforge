"""The evaluator registry: every evaluator is addressed as `name@version`.

A run records the exact `name@version` of each evaluator it used, and every
stored result records the version that produced it, so changing an
evaluator's behavior means registering a new version -- never editing an
existing one in place, which would silently reinterpret historical results.
"""

from __future__ import annotations

from agentforge_evaluators import answer_match, operational, retrieval
from agentforge_evaluators.base import EvaluatorFn, EvaluatorKind, EvaluatorSpec


class UnknownEvaluatorError(ValueError):
    pass


_REGISTRY: dict[tuple[str, str], EvaluatorSpec] = {}


def register(name: str, version: str, kind: EvaluatorKind, description: str, fn: EvaluatorFn) -> EvaluatorSpec:
    key = (name, version)
    if key in _REGISTRY:
        raise ValueError(f"evaluator {name}@{version} is already registered")
    spec = EvaluatorSpec(name=name, version=version, kind=kind, description=description, fn=fn)
    _REGISTRY[key] = spec
    return spec


def _version_key(version: str) -> tuple:
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


register(
    "exact_match", "1.0.0", "quality",
    "answer == expected_answer after casefold + whitespace normalization (1.0 or 0.0)",
    answer_match.exact_match,
)
register(
    "answer_contains", "1.0.0", "quality",
    "fraction of expected_answer_contains phrases found in the answer (case-insensitive); passes only if all found",
    answer_match.answer_contains,
)
register(
    "answer_regex", "1.0.0", "quality",
    "re.search(expected_answer_regex, answer) (1.0 or 0.0)",
    answer_match.answer_regex,
)
register(
    "heuristic_context_precision", "1.0.0", "quality",
    "|retrieved ∩ expected_relevant| / |retrieved| over doc IDs (0.0 when nothing retrieved); not RAGAS",
    retrieval.context_precision,
)
register(
    "heuristic_context_recall", "1.0.0", "quality",
    "|retrieved ∩ expected_relevant| / |expected_relevant| over doc IDs; n/a when nothing expected; not RAGAS",
    retrieval.context_recall,
)
register(
    "citation_correctness", "1.0.0", "quality",
    "|cited ∩ expected_relevant| / |cited|; citing a never-retrieved doc fails the case",
    retrieval.citation_correctness,
)
register(
    "latency", "1.0.0", "measurement",
    "wall-clock adapter call time in ms; passes/fails only when a max_latency_ms budget is set",
    operational.latency,
)
register(
    "token_usage", "1.0.0", "measurement",
    "input + output tokens as reported by the adapter; never estimated",
    operational.token_usage,
)
register(
    "estimated_cost", "1.0.0", "measurement",
    "ESTIMATED USD: adapter-reported tokens x per-model rates from the pricing config",
    operational.estimated_cost,
)

DEFAULT_EVALUATORS: list[str] = [s.key for s in list_evaluators()]
