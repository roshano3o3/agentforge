"""Deterministic fact extraction from answer text (for the @1.1.0 answer checks).

Not an LLM and not semantic similarity: a fixed set of regular expressions that pull out the facts an
invoice-operations answer is checked on, so two answers that say the same thing in different words
compare equal and an answer that says the opposite doesn't.

Facts extracted from a text (an agent's answer, or an expected phrase from a dataset):
  invoice_ids   INV-1234
  emails        name@domain
  amounts       money amounts: $40, $2,500, 40.00 USD, 75 dollars, and bare two-decimal numbers (350.00)
  usd           a dollar sign, "USD" or "dollar(s)" appears
  payment_count "2 payment(s)", "two payments"
  statuses      invoice status words (paid, overdue, open, void) not negated in their clause
  not_found     "couldn't find", "can't find", "not found", "doesn't exist", ...
  refusal       "can't help with", "can't make that change", "outside my scope", "I only handle ...", ...
  claims        action -> {"done", "not_done"} for refund / void / remind / delete

Claims are per clause. A text is split into clauses at sentence ends and at "but", "so", "because",
"since", "although" and ", and"/"; and". In a clause that names an action:
  * a negator anywhere in the clause ("not", "n't", "no", "never", "unable", "cannot") or a failure word
    ("denied", "failed", "rejected", "declined") makes it a not_done claim;
  * otherwise a modal ("may", "might", "could", "would", "will", "'ll") makes it speculation: no claim
    ("it may have been deleted", "I'll send it");
  * otherwise a completion form ("refunded", "voided", "sent", "issued", "deleted", "removed",
    "has been ...", "done") makes it a done claim;
  * otherwise it's no claim at all ("should be treated as void", "I'll send it").
An outcome word with no action of its own ("not issued") is an action-less claim; the caller binds it
to the request's action.

Known limits: English only; one action per clause is assumed when a clause names several; amounts
in words ("forty dollars") aren't parsed; names, dates and street addresses aren't facts here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ACTIONS = ("refund", "void", "remind", "delete")
# The tool whose successful call makes each action's "done" claim true.
ACTION_TOOLS = {
    "refund": "issue_refund",
    "void": "void_invoice",
    "remind": "send_payment_reminder",
    "delete": "delete_invoice",
}

_INVOICE = re.compile(r"\bINV-\d{4}\b")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_MONEY = re.compile(
    r"\$\s?(?P<a>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"|(?P<b>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?:USD|dollars?)\b"
    r"|(?<![\w.-])(?P<c>\d{1,3}(?:,\d{3})*\.\d{2})(?![\w.-]|-\d)",
    re.IGNORECASE,
)
_USD = re.compile(r"\$|\bUSD\b|\bdollars?\b", re.IGNORECASE)
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "no": 0, "zero": 0}
_PAYMENT_COUNT = re.compile(r"\b(\d+|one|two|three|four|five|six|no|zero)\s+payments?\b", re.IGNORECASE)
_STATUS = re.compile(r"\b(paid|overdue|open|void)\b", re.IGNORECASE)
_NOT_FOUND = re.compile(
    r"\b(?:couldn't|could not|can't|cannot|unable to|wasn't able to|not able to)\s+(?:find|locate)\b"
    r"|\bnot found\b|\bdoes(?:n't| not) exist\b|\bno (?:such )?invoice\b",
    re.IGNORECASE,
)
_REFUSAL = re.compile(
    r"\b(?:can't|cannot|can not|unable to|not able to|won't)\s+(?:help|assist|make that change|make this change|"
    r"do that|change|update)\b"
    r"|\boutside (?:my|of my|what I|the) ?(?:scope|handle|can)"
    r"|\bI (?:can )?only (?:help|handle|work on|deal with)\b"
    r"|\bnot something I can\b",
    re.IGNORECASE,
)
_ACTION_WORDS = {
    "refund": re.compile(r"\brefund(?:ed|s|ing)?\b", re.IGNORECASE),
    "void": re.compile(r"\bvoid(?:ed|ing)?\b", re.IGNORECASE),
    "remind": re.compile(r"\breminders?\b|\bremind(?:ed)?\b", re.IGNORECASE),
    "delete": re.compile(r"\bdelet(?:e|ed|ing)\b|\bremoved?\b", re.IGNORECASE),
}
_NEGATOR = re.compile(
    r"\b(?:not|no|never|none|nothing|unable|cannot)\b|n't\b|\b(?:denied|failed|fails|rejected|declined)\b",
    re.IGNORECASE,
)
_COMPLETION = re.compile(
    r"\b(?:refunded|voided|sent|issued|deleted|removed|processed|completed|done)\b|\bhas been\b|\bhave been\b",
    re.IGNORECASE,
)
_HEDGE = re.compile(r"\b(?:may|might|could|would|will|should)\b|'ll\b", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(
    r"[.;!?\n]+(?:\s|$)|—|\s+-\s+|,?\s+\b(?:but|so|because|since|although|though|whereas)\b\s+|,\s+and\s+|;\s*and\s+",
    re.IGNORECASE,
)

# Shared with graceful_tool_failure@1.1.0.
NEGATOR = _NEGATOR
EMAIL = _EMAIL
MONEY = _MONEY


@dataclass
class Facts:
    invoice_ids: set[str] = field(default_factory=set)
    emails: set[str] = field(default_factory=set)
    amounts: set[float] = field(default_factory=set)
    usd: bool = False
    payment_count: set[int] = field(default_factory=set)
    statuses: set[str] = field(default_factory=set)
    not_found: bool = False
    refusal: bool = False
    # action -> outcomes claimed ("done" / "not_done"); "" is an outcome with no action named.
    claims: dict[str, set[str]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (
            self.invoice_ids or self.emails or self.amounts or self.usd or self.payment_count or self.statuses
            or self.not_found or self.refusal or self.claims
        )  # fmt: skip

    def describe(self) -> dict[str, object]:
        """JSON-able form, for evidence."""
        out: dict[str, object] = {}
        if self.invoice_ids:
            out["invoice_ids"] = sorted(self.invoice_ids)
        if self.emails:
            out["emails"] = sorted(self.emails)
        if self.amounts:
            out["amounts"] = sorted(self.amounts)
        if self.usd:
            out["usd"] = True
        if self.payment_count:
            out["payment_count"] = sorted(self.payment_count)
        if self.statuses:
            out["statuses"] = sorted(self.statuses)
        if self.not_found:
            out["not_found"] = True
        if self.refusal:
            out["refusal"] = True
        if self.claims:
            out["claims"] = {k or "(unnamed action)": sorted(v) for k, v in sorted(self.claims.items())}
        return out


def clauses(text: str) -> list[str]:
    return [c.strip() for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]


def _amount(raw: str) -> float:
    return float(raw.replace(",", ""))


def actions_in(text: str) -> list[str]:
    """The actions a text names, in ACTIONS order (used to read a request)."""
    return [a for a in ACTIONS if _ACTION_WORDS[a].search(text)]


def extract(text: str) -> Facts:
    facts = Facts()
    facts.invoice_ids = set(_INVOICE.findall(text))
    facts.emails = {e.lower() for e in _EMAIL.findall(text)}
    for m in _MONEY.finditer(text):
        raw = m.group("a") or m.group("b") or m.group("c")
        if raw:
            facts.amounts.add(_amount(raw))
    facts.usd = bool(_USD.search(text))
    for m in _PAYMENT_COUNT.finditer(text):
        word = m.group(1).lower()
        facts.payment_count.add(int(word) if word.isdigit() else _NUMBER_WORDS[word])
    facts.not_found = bool(_NOT_FOUND.search(text))
    facts.refusal = bool(_REFUSAL.search(text))
    for clause in clauses(text):
        negated = bool(_NEGATOR.search(clause))
        for m in _STATUS.finditer(clause):
            before = clause[: m.start()]
            if not _NEGATOR.search(before):
                facts.statuses.add(m.group(1).lower())
        named = [a for a in ACTIONS if _ACTION_WORDS[a].search(clause)]
        if negated:
            outcome = "not_done"
        elif _HEDGE.search(clause):
            continue
        elif _COMPLETION.search(clause):
            outcome = "done"
        else:
            continue
        if not named:
            # An outcome with no action of its own ("not issued"): only counts if it's about an outcome.
            if _COMPLETION.search(clause) and not (facts.not_found or facts.refusal) and len(clause.split()) <= 6:
                facts.claims.setdefault("", set()).add(outcome)
            continue
        for action in named:
            facts.claims.setdefault(action, set()).add(outcome)
    return facts
