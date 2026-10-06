"""Original result vs replay: verdicts, trajectory, answer, latency and tokens.

Pure: takes both sides as plain data (what the API already loads) and
returns a ReplayDiffOut. Nothing here judges the agent; it lines up what was
stored for the original case and for the replay.

Trajectory alignment: steps are matched in order by (kind, tool name) with
difflib's longest-matching-blocks algorithm. A matched pair is `unchanged`
or `changed` (args / result / error / output / retrieved docs differ); a
step only on one side is `removed` (original only) or `added` (replay only).
So "the same tool called with different args" is a change, and "a different
tool" is one removal plus one addition.

`identical` (the determinism check) ignores only what is measured rather
than produced: latency, the latency evaluator's measured value and its
reason (which quotes it), step timings and span ids.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from agentforge_core.schemas import (
    AgentStepOut,
    AnswerDiff,
    EvaluatorChange,
    MetricBrief,
    MetricScoreOut,
    NumberChange,
    ReplayDiffOut,
    ResultStatus,
    StepBrief,
    StepChange,
    TextSegment,
)

# Measured, not produced: its value (and the reason quoting it) varies run to run.
TIMING_EVALUATORS = frozenset({"latency"})
_STEP_FIELDS = ("args", "result", "error", "output", "retrieved_doc_ids")
_WORDS = re.compile(r"\s+|[^\s]+")


@dataclass
class CaseSide:
    status: ResultStatus
    passed: bool | None
    answer: str | None
    latency_ms: float | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    retrieved_doc_ids: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    error_type: str | None = None
    metrics: Sequence[MetricScoreOut] = ()
    steps: Sequence[AgentStepOut] = ()


def _brief_metric(m: MetricScoreOut) -> MetricBrief:
    return MetricBrief(
        evaluator_version=m.evaluator_version,
        passed=m.passed,
        score=m.score,
        value=m.value,
        unit=m.unit,
        reason=m.reason,
    )


def _metric_change(name: str, before: MetricScoreOut | None, after: MetricScoreOut | None) -> EvaluatorChange:
    b = _brief_metric(before) if before else None
    a = _brief_metric(after) if after else None
    if b is None:
        change = "added"
    elif a is None:
        change = "removed"
    elif b.passed is False and a.passed is True:
        change = "fixed"
    elif b.passed is True and a.passed is False:
        change = "regressed"
    elif _same_metric(name, b, a):
        change = "unchanged"
    else:
        change = "changed"
    return EvaluatorChange(evaluator_name=name, change=change, before=b, after=a)  # type: ignore[arg-type]


def _same_metric(name: str, b: MetricBrief, a: MetricBrief) -> bool:
    if (b.evaluator_version, b.passed, b.score, b.unit) != (a.evaluator_version, a.passed, a.score, a.unit):
        return False
    if name in TIMING_EVALUATORS:
        return True
    return (b.value, b.reason) == (a.value, a.reason)


def _brief_step(s: AgentStepOut) -> StepBrief:
    return StepBrief(
        step_index=s.step_index,
        kind=s.kind,
        name=s.name,
        args=s.args,
        result=s.result,
        error=s.error,
        output=s.output,
        retrieved_doc_ids=s.retrieved_doc_ids,
    )


def trajectory_diff(before: Sequence[AgentStepOut], after: Sequence[AgentStepOut]) -> list[StepChange]:
    keys_b = [(s.kind, s.name) for s in before]
    keys_a = [(s.kind, s.name) for s in after]
    out: list[StepChange] = []

    def removed(s: AgentStepOut) -> None:
        out.append(StepChange(op="removed", kind=s.kind, name=s.name, before=_brief_step(s), after=None))

    def added(s: AgentStepOut) -> None:
        out.append(StepChange(op="added", kind=s.kind, name=s.name, before=None, after=_brief_step(s)))

    for tag, i1, i2, j1, j2 in SequenceMatcher(a=keys_b, b=keys_a, autojunk=False).get_opcodes():
        if tag == "equal":
            for sb, sa in zip(before[i1:i2], after[j1:j2], strict=True):
                changed = [f for f in _STEP_FIELDS if getattr(sb, f) != getattr(sa, f)]
                out.append(
                    StepChange(
                        op="changed" if changed else "unchanged",
                        kind=sb.kind,
                        name=sb.name,
                        before=_brief_step(sb),
                        after=_brief_step(sa),
                        changed_fields=changed,
                    )
                )
        else:  # delete / insert / replace: different steps on each side
            for s in before[i1:i2]:
                removed(s)
            for s in after[j1:j2]:
                added(s)
    return out


def answer_diff(before: str | None, after: str | None) -> AnswerDiff:
    words_b = _WORDS.findall(before or "")
    words_a = _WORDS.findall(after or "")
    segments: list[TextSegment] = []

    def push(op: str, text: str) -> None:
        if not text:
            return
        if segments and segments[-1].op == op:
            segments[-1].text += text
        else:
            segments.append(TextSegment(op=op, text=text))  # type: ignore[arg-type]

    for tag, i1, i2, j1, j2 in SequenceMatcher(a=words_b, b=words_a, autojunk=False).get_opcodes():
        if tag == "equal":
            push("equal", "".join(words_b[i1:i2]))
        else:
            push("delete", "".join(words_b[i1:i2]))
            push("insert", "".join(words_a[j1:j2]))
    return AnswerDiff(before=before, after=after, changed=before != after, segments=segments)


def _number(before: float | None, after: float | None) -> NumberChange:
    delta = after - before if before is not None and after is not None else None
    return NumberChange(before=before, after=after, delta=delta)


def compute_diff(before: CaseSide, after: CaseSide) -> ReplayDiffOut:
    by_name_b = {m.evaluator_name: m for m in before.metrics}
    by_name_a = {m.evaluator_name: m for m in after.metrics}
    names = list(by_name_b) + [n for n in by_name_a if n not in by_name_b]
    evaluators = [_metric_change(n, by_name_b.get(n), by_name_a.get(n)) for n in names]
    trajectory = trajectory_diff(before.steps, after.steps)
    answer = answer_diff(before.answer, after.answer)

    differences: list[str] = []
    if before.status != after.status:
        differences.append(f"status {before.status.value} -> {after.status.value}")
    if before.error_type != after.error_type:
        differences.append(f"error type {before.error_type} -> {after.error_type}")
    if before.passed != after.passed:
        differences.append(f"case verdict {before.passed} -> {after.passed}")
    if answer.changed:
        differences.append("final answer")
    for label, b, a in (
        ("retrieved docs", before.retrieved_doc_ids, after.retrieved_doc_ids),
        ("citations", before.citations, after.citations),
        ("input tokens", before.input_tokens, after.input_tokens),
        ("output tokens", before.output_tokens, after.output_tokens),
    ):
        if b != a:
            differences.append(label)
    for step in trajectory:
        if step.op != "unchanged":
            index = (step.before or step.after).step_index  # type: ignore[union-attr]
            what = f" ({', '.join(step.changed_fields)})" if step.changed_fields else ""
            differences.append(f"step {index} {step.name or step.kind}: {step.op}{what}")
    for ev in evaluators:
        if ev.change != "unchanged":
            differences.append(f"evaluator {ev.evaluator_name}: {ev.change}")

    return ReplayDiffOut(
        identical=not differences,
        differences=differences,
        before_status=before.status,
        after_status=after.status,
        before_passed=before.passed,
        after_passed=after.passed,
        evaluators=evaluators,
        trajectory=trajectory,
        answer=answer,
        latency_ms=_number(before.latency_ms, after.latency_ms),
        input_tokens=_number(before.input_tokens, after.input_tokens),
        output_tokens=_number(before.output_tokens, after.output_tokens),
    )
