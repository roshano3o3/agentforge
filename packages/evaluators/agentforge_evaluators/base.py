"""Core types shared by every evaluator.

Every evaluator here is deterministic Python: no model call, no network,
no LLM-as-judge. An evaluator takes one case's inputs + the adapter's output
(`EvalInput`) and the run's settings (`EvalConfig`), and returns a
`MetricOutcome`: a 0..1 `score` (quality evaluators) and/or a measured
`value` + `unit` (measurement evaluators such as latency, tokens, cost),
a `passed` verdict (None when not applicable / no budget configured), a
human-readable `reason`, and `evidence` holding every input needed to
re-derive the result without re-running anything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from agentforge_evaluators.pricing import ModelPrice

EvaluatorKind = Literal["quality", "measurement"]


@dataclass(frozen=True)
class EvalInput:
    input: str
    expected_answer: str | None
    expected_answer_contains: list[str]
    expected_answer_regex: str | None
    expected_context: list[str]
    answer: str
    retrieved_doc_ids: list[str]
    citations: list[str]
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


@dataclass(frozen=True)
class EvalConfig:
    threshold: float = 0.7
    max_latency_ms: float | None = None
    pricing: Mapping[str, ModelPrice] = field(default_factory=dict)


@dataclass
class MetricOutcome:
    score: float | None
    passed: bool | None
    reason: str
    evidence: dict = field(default_factory=dict)
    value: float | None = None
    unit: str | None = None
    labels: list[str] = field(default_factory=list)


EvaluatorFn = Callable[[EvalInput, EvalConfig], MetricOutcome]


@dataclass(frozen=True)
class EvaluatorSpec:
    name: str
    version: str
    kind: EvaluatorKind
    description: str
    fn: EvaluatorFn

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


def not_applicable(reason: str, **evidence: object) -> MetricOutcome:
    """A result that neither passes nor fails the case (e.g. the test case
    has no expected_answer, so exact_match has nothing to compare)."""
    return MetricOutcome(score=None, passed=None, reason=f"not applicable: {reason}", evidence=dict(evidence))
