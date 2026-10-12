"""Agreement of evaluator versions with a hand-labeled calibration set (`agentforge calibration`).

Offline: each item (datasets/calibration/*.yaml) carries the input, final answer, recorded steps, the
check's params and safety expectations, and a label (correct / incorrect) written by a person. The
evaluator is called on that stored output exactly as the worker would call it; nothing is run against an
agent and nothing is stored. A check's "positive" is a FAIL verdict:

  false positive  the check fails an output labeled correct   (a good answer flagged)
  false negative  the check passes an output labeled incorrect (a bad answer let through)

An item the evaluator calls not applicable counts as a disagreement and is listed separately.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agentforge_evaluators import EvalConfig, EvalInput, TrajectoryStep, resolve


@dataclass
class Agreement:
    evaluator: str  # name@version
    items: int = 0
    agree: int = 0
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)
    not_applicable: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.agree / self.items if self.items else 0.0


def load(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))["items"]
    return items


def eval_input(item: Mapping[str, Any]) -> EvalInput:
    steps = tuple(
        TrajectoryStep(
            index=s["step_index"],
            kind=s["kind"],
            name=s.get("name") or "",
            args=dict(s.get("args") or {}),
            result=s.get("result"),
            error=s.get("error"),
        )
        for s in item["steps"]
    )
    return EvalInput(
        input=item["input"],
        expected_answer=None,
        expected_context=[],
        answer=item["answer"],
        retrieved_doc_ids=[],
        citations=[],
        latency_ms=0.0,
        steps=steps,
        safety=item.get("safety"),
    )


def verdict(item: Mapping[str, Any], version: str) -> bool | None:
    """The evaluator's pass/fail on the item (None: not applicable)."""
    spec = resolve(f"{item['check']}@{version}")
    return spec.fn(eval_input(item), EvalConfig(), dict(item.get("params") or {})).passed


def agreement(items: Iterable[Mapping[str, Any]], version: str, checks: set[str] | None = None) -> list[Agreement]:
    """Per check name: agreement of `<check>@<version>` with the labels."""
    by_check: dict[str, Agreement] = {}
    for item in items:
        if checks is not None and item["check"] not in checks:
            continue
        a = by_check.setdefault(item["check"], Agreement(f"{item['check']}@{version}"))
        a.items += 1
        passed = verdict(item, version)
        correct = item["label"] == "correct"
        if passed is None:
            a.not_applicable.append(item["id"])
        elif passed == correct:
            a.agree += 1
        elif correct:
            a.false_positives.append(item["id"])
        else:
            a.false_negatives.append(item["id"])
    return [by_check[k] for k in sorted(by_check)]


def total(rows: Iterable[Agreement], label: str) -> Agreement:
    out = Agreement(label)
    for r in rows:
        out.items += r.items
        out.agree += r.agree
        out.false_positives += r.false_positives
        out.false_negatives += r.false_negatives
        out.not_applicable += r.not_applicable
    return out
