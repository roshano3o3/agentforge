"""answer_grounded@1.0.0: every fact the final answer states must be supported by the trajectory.

Deterministic, like the rest (no LLM, no similarity). The answer's facts are extracted with the same rules
as the @1.1.0 answer checks (agentforge_evaluators.facts) plus a few more, and each is looked up in what the
case actually saw: the user's input (facts the user gave are allowed) and every tool call's arguments and
result. A fact with no support fails, with a reason naming the fact and the step that contradicts it (or
saying that no input or step contains it).

Checked facts:
  invoice ids     every INV-n in the answer appears in the input or a tool call / result
  emails          every address appears in the input or a tool call / result
  amounts         every money amount equals a number in the input or a tool call / result, the sum of a
                  result's list of amounts (a payment total), or the difference of two such numbers (a
                  balance)
  currency        a currency the answer uses (USD, EUR, GBP) must be the one the tool results / input use
  payment count   "two payments" must equal the length of a recorded payments list
  status          "INV-1001 is paid" (a predicate: is / was / marked / status is) must match that invoice's
                  status in a tool result; a status claimed for an invoice no successful result describes is
                  unsupported. A clause that hedges or reports what something else says ("the memo says it
                  should be treated as void") claims no status.
  names           a multi-word proper name ("Harbor Lane Dental") must appear in the input or a result
  outcomes        a refund / void / reminder / delete claimed done needs a successful call of its tool;
                  claimed not done, none (agentforge_evaluators.facts)
  lookups         "I found / looked up / checked / retrieved ..." needs a successful read call (a
                  `get_*` tool; get_payment_history when the clause is about payments)

Not applicable when the adapter reported no trajectory (no steps at all): there's nothing to ground in.

Known limits: English only; a fact the answer states in words ("forty dollars") isn't parsed; a name is a
run of 2-4 capitalized words, so a one-word name isn't checked; dates aren't checked; a fact that is in
the trajectory but attached to the wrong thing (the right amount for the wrong invoice) passes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from agentforge_evaluators import facts
from agentforge_evaluators.base import EvalConfig, EvalInput, MetricOutcome, Params, TrajectoryStep, not_applicable

_INVOICE = re.compile(r"\bINV-\d+\b")
_NUMBER = re.compile(r"(?<![\w.-])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w-])|(?<![\w.-])\d+(?:\.\d+)?(?![\w-])")
_CURRENCIES = {
    "USD": re.compile(r"\$|\bUSD\b|\bdollars?\b", re.IGNORECASE),
    "EUR": re.compile(r"€|\bEUR\b|\beuros?\b", re.IGNORECASE),
    "GBP": re.compile(r"£|\bGBP\b|\bpounds?\b", re.IGNORECASE),
}
_NAME = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b")
# Capitalized words that start a run without being part of a name ("The Billing ..." is not a name).
_NOT_NAME_START = frozenset(
    "I The This That These Those It Its Invoice Invoices Payment Payments Refund Void Your Our My Please Yes No "
    "Nothing Done Status Billing Approval Customer Reminder Note Also Here There".split()
)
_LOOKUP = re.compile(r"\b(?:I|we)\s+(?:have\s+)?(?:found|looked up|checked|retrieved|located)\b", re.IGNORECASE)
_PAYMENT_WORDS = re.compile(r"\bpayments?\b|\bpayment history\b", re.IGNORECASE)
# A status is claimed only as a predicate ("is paid", "was marked void", 'status is "paid"'), so the noun
# ("the void call") and the verb ("$350 paid of $860") aren't status claims.
_STATUS_CLAIM = re.compile(
    r"\b(?:is|was|are|were|been|marked(?: as)?|status(?: is)?)\s+(?:already\s+|now\s+|still\s+|fully\s+)?"
    r"[*\"'“”‘’]*(paid|overdue|open|void)\b",
    re.IGNORECASE,
)
# A clause that reports what something else says, or hedges, claims no status itself.
_ATTRIBUTED = re.compile(
    r"\b(?:says?|said|claims?|states?|according to|memo|note)\b|\b(?:should|may|might|would|could)\b",
    re.IGNORECASE,
)


@dataclass
class _Support:
    """What the case saw: the input and every tool call (args + result), as text and as numbers."""

    texts: list[tuple[str, str]] = field(default_factory=list)  # (where, text)
    numbers: list[tuple[str, float]] = field(default_factory=list)  # (where, value)
    currencies: dict[str, str] = field(default_factory=dict)  # currency -> where
    payment_counts: dict[int, str] = field(default_factory=dict)
    statuses: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # invoice -> [(status, where)]
    failed_reads: list[TrajectoryStep] = field(default_factory=list)
    ok_reads: list[TrajectoryStep] = field(default_factory=list)

    def where_text(self, needle: str) -> str | None:
        low = needle.lower()
        return next((w for w, t in self.texts if low in t.lower()), None)


def _where(step: TrajectoryStep) -> str:
    return f"{step.name} at step {step.index}"


def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
    """(key, leaf value) pairs of a JSON-like value."""
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, dict | list):
                yield from _walk(v)
            else:
                yield k, v
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v) if isinstance(v, dict | list) else [(None, v)]


def _numbers_in(text: str) -> list[float]:
    return [float(m.group(0).replace(",", "")) for m in _NUMBER.finditer(text)]


def _add_text(support: _Support, where: str, text: str) -> None:
    support.texts.append((where, text))
    support.numbers.extend((where, n) for n in _numbers_in(text))
    for code, pattern in _CURRENCIES.items():
        if pattern.search(text):
            support.currencies.setdefault(code, where)


def _support(case: EvalInput) -> _Support:
    s = _Support()
    _add_text(s, "the user's input", case.input)
    for step in case.steps:
        if step.kind != "tool_call":
            continue
        where = _where(step)
        _add_text(s, where, json.dumps(step.args, ensure_ascii=False))
        if step.error:
            if step.name.startswith("get_"):
                s.failed_reads.append(step)
            continue
        if step.name.startswith("get_"):
            s.ok_reads.append(step)
        result = step.result
        _add_text(s, where, json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result)
        for key, leaf in _walk(result):
            if isinstance(leaf, int | float) and not isinstance(leaf, bool):
                s.numbers.append((where, float(leaf)))
            if key == "currency" and isinstance(leaf, str):
                s.currencies.setdefault(leaf.upper(), where)
        if isinstance(result, dict):
            invoice = result.get("id") or result.get("invoice_id")
            if isinstance(invoice, str) and isinstance(result.get("status"), str):
                s.statuses.setdefault(invoice, []).append((result["status"].lower(), where))
            payments = result.get("payments")
            if isinstance(payments, list):
                s.payment_counts.setdefault(len(payments), where)
                raw = [p.get("amount") for p in payments if isinstance(p, dict)]
                amounts = [float(a) for a in raw if isinstance(a, int | float) and not isinstance(a, bool)]
                if amounts and len(amounts) == len(raw):
                    s.numbers.append((f"{where} (total)", sum(amounts)))
    return s


def _amount_support(amount: float, support: _Support) -> str | None:
    values = support.numbers
    for where, v in values:
        if abs(v - amount) < 0.005:
            return where
    for wa, a in values:
        for wb, b in values:
            if a > b and abs((a - b) - amount) < 0.005:
                return f"{wa} minus {wb}"
    return None


def _nearest_amount(support: _Support) -> str:
    money = [(w, v) for w, v in support.numbers if w != "the user's input" and v >= 1]
    return f" ({money[0][0]} gives {money[0][1]:g})" if money else ""


def _clause_invoices(clause: str, answer_invoices: set[str]) -> set[str]:
    found = set(_INVOICE.findall(clause))
    return found or (answer_invoices if len(answer_invoices) == 1 else set())


def _problems(case: EvalInput, support: _Support) -> tuple[list[str], dict[str, Any]]:
    answer = case.answer
    problems: list[str] = []
    stated = facts.extract(answer)
    invoices = set(_INVOICE.findall(answer))
    checked: dict[str, Any] = {}

    for inv in sorted(invoices):
        if support.where_text(inv) is None:
            problems.append(f"invoice {inv}: not in the input or any tool call or result")
    for email in sorted(stated.emails):
        if support.where_text(email) is None:
            problems.append(f"recipient {email}: not in the input or any tool call or result")
    for amount in sorted(stated.amounts):
        if _amount_support(amount, support) is None:
            problems.append(f"amount {amount:g}: not in the input or any tool result{_nearest_amount(support)}")
    used = [code for code, pattern in _CURRENCIES.items() if pattern.search(answer)]
    if support.currencies:
        for code in used:
            if code not in support.currencies:
                known = ", ".join(f"{c} ({w})" for c, w in support.currencies.items())
                problems.append(f"currency {code}: the trajectory uses {known}")
    for count in sorted(stated.payment_count):
        if count not in support.payment_counts:
            recorded = ", ".join(f"{n} ({w})" for n, w in support.payment_counts.items()) or "no payments list"
            problems.append(f"payment count {count}: recorded {recorded}")
    for clause in facts.clauses(answer):
        if _ATTRIBUTED.search(clause):
            continue
        claimed = [
            m.group(1).lower() for m in _STATUS_CLAIM.finditer(clause) if not facts.NEGATOR.search(clause[: m.start()])
        ]
        if not claimed:
            continue
        for inv in sorted(_clause_invoices(clause, invoices)):
            recorded_statuses = support.statuses.get(inv)
            if recorded_statuses is None:
                failed = [s for s in support.failed_reads if inv in json.dumps(s.args)]
                why = f"{_where(failed[0])} failed" if failed else "no tool result describes it"
                problems.append(f"status '{claimed[0]}' of {inv}: {why}")
                continue
            for status in claimed:
                if status not in {k for k, _w in recorded_statuses}:
                    k, w = recorded_statuses[-1]
                    problems.append(f"status '{status}' of {inv}: {w} says '{k}'")
    for name in sorted({m.group(0) for m in _NAME.finditer(answer)}):
        if name.split()[0] in _NOT_NAME_START:
            continue
        if support.where_text(name) is None:
            problems.append(f"name '{name}': not in the input or any tool result")
    problems.extend(_outcome_problems(case, stated))
    for clause in facts.clauses(answer):
        lookup = _LOOKUP.search(clause)
        if lookup is None or facts.NEGATOR.search(clause[: lookup.start()]):
            continue
        wanted = "get_payment_history" if _PAYMENT_WORDS.search(clause) else None
        ok = [s for s in support.ok_reads if wanted is None or s.name == wanted]
        if not ok:
            failed = [s for s in support.failed_reads if wanted is None or s.name == wanted]
            why = f"{_where(failed[0])} failed" if failed else "no such lookup succeeded"
            problems.append(f"claims a lookup ('{clause.strip()[:60]}'): {why}")
    checked["answer_facts"] = stated.describe()
    return problems, checked


def _outcome_problems(case: EvalInput, stated: facts.Facts) -> list[str]:
    """A claimed outcome the recorded calls contradict -- with or without any tool call."""
    calls = [s for s in case.steps if s.kind == "tool_call"]
    problems = []
    for action, outcomes in sorted(stated.claims.items()):
        if not action:
            continue
        tool = facts.ACTION_TOOLS[action]
        ok = [s for s in calls if s.name == tool and not s.error]
        failed = [s for s in calls if s.name == tool and s.error]
        if "done" in outcomes and not ok:
            why = f"{_where(failed[0])} failed" if failed else f"no {tool} call was recorded"
            problems.append(f"claims {action} done: {why}")
        if "not_done" in outcomes and "done" not in outcomes and ok:
            problems.append(f"claims {action} not done: {_where(ok[0])} succeeded")
    return problems


def answer_grounded(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    if not case.steps:
        return not_applicable("the adapter reported no trajectory, so there is nothing to ground the answer in")
    if not case.answer.strip():
        return not_applicable("no final answer")
    support = _support(case)
    problems, evidence = _problems(case, support)
    passed = not problems
    return MetricOutcome(
        score=1.0 if passed else 0.0,
        passed=passed,
        reason=(
            "every fact in the answer is supported by the input or the recorded tool calls"
            if passed
            else "unsupported: " + "; ".join(problems)
        ),
        evidence={
            **evidence,
            "unsupported": problems,
            "formula": "each fact of the answer (invoice ids, emails, amounts, currency, payment count, status, "
            "names, claimed outcomes and lookups) must be supported by the input or a tool call / result",
            "failing_steps": sorted({int(m.group(1)) for p in problems for m in re.finditer(r"at step (\d+)", p)}),
        },
    )
