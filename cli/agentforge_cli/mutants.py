"""Mutation calibration set for the answer checks: answers known to be wrong, made from known-good ones.

`python -m agentforge_cli.mutants` reads the held-out calibration set
(datasets/calibration/answer_checks_holdout_v1.yaml, every item labeled correct) and writes
datasets/calibration/answer_checks_mutants_v1.yaml: copies of those answers with exactly one fact broken,
each labeled incorrect. Steps, params and safety expectations are the original's, so the recorded
trajectory still says what really happened and the mutant's claim contradicts it.

Mutation types (each a fixed text rule; `random.Random(SEED)` only picks which items and which token):
  wrong_amount           one money amount + 100 (format kept: $, commas, cents)
  wrong_currency         USD -> EUR, or $ -> EUR sign when no USD is written
  wrong_invoice_id       one invoice id -> the next id (INV-1006 -> INV-1007), every occurrence
  wrong_recipient        one customer email or name -> another customer's
  success_after_failure  the whole answer replaced by a claim of the result the failed tool call prevented
  unperformed_action     a sentence claiming a write action (reminder/refund/void) the steps don't contain
  dropped_negation       one negated outcome turned into a done claim ("couldn't find" -> "found")

Deterministic: same input file and SEED -> byte-identical output (a test regenerates and compares).
Nothing here runs an evaluator.
"""

from __future__ import annotations

import random
import re
import sys
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

SEED = 7
REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "datasets" / "calibration" / "answer_checks_holdout_v1.yaml"
TARGET = REPO / "datasets" / "calibration" / "answer_checks_mutants_v1.yaml"

# Mutants per type (30 in all).
QUOTA = {
    "wrong_amount": 5,
    "wrong_currency": 4,
    "wrong_invoice_id": 5,
    "wrong_recipient": 4,
    "success_after_failure": 5,
    "unperformed_action": 4,
    "dropped_negation": 3,
}

CUSTOMERS = (
    ("Brightwater Cafe", "accounts@brightwater.example"),
    ("Kestrel Robotics", "ap@kestrel-robotics.example"),
    ("Harbor Lane Dental", "billing@harborlane.example"),
)
AMOUNT = re.compile(r"(?<![\w.-])(\$?)(\d{1,3}(?:,\d{3})*|\d+)(\.\d{2})?(?![\w-])")
INVOICE = re.compile(r"\bINV-\d{4}\b")
WRITE_TOOLS = ("send_payment_reminder", "issue_refund", "void_invoice")
NEGATIONS: tuple[tuple[str, str], ...] = (
    (r"\bI couldn't find\b", "I found"),
    (r"\bI couldn't void\b", "I voided"),
    (r"\bI couldn't look up\b", "I looked up"),
    (r"\bI couldn't check\b", "I checked"),
    (r"\bI didn't send a reminder\b", "I sent a reminder"),
    (r"\bno reminder was sent\b", "a reminder was sent"),
    (r"\bI wasn't able to retrieve\b", "I retrieved"),
    (r"\bI wasn't able to check\b", "I checked"),
)

Mutation = tuple[str, str, str]  # (new answer, what changed, the original text that was changed)


def _fmt_amount(dollar: str, whole: str, cents: str | None, add: int) -> str:
    value = int(whole.replace(",", "")) + add
    text = f"{value:,}" if "," in whole else str(value)
    return f"{dollar}{text}{cents or ''}"


def _amounts(answer: str) -> list[re.Match[str]]:
    """Money amounts: written with a $ or with cents (dates, ids and counts don't qualify)."""
    return [m for m in AMOUNT.finditer(answer) if m.group(1) or m.group(3)]


def wrong_amount(item: Mapping[str, Any], rng: random.Random) -> Mutation | None:
    found = _amounts(item["answer"])
    if not found:
        return None
    m = rng.choice(found)
    new = _fmt_amount(m.group(1), m.group(2), m.group(3), 100)
    a = item["answer"]
    return a[: m.start()] + new + a[m.end() :], f"amount {m.group(0)} -> {new}", m.group(0)


def wrong_currency(item: Mapping[str, Any], rng: random.Random) -> Mutation | None:
    a = item["answer"]
    if "USD" in a:
        return a.replace("USD", "EUR"), "currency USD -> EUR", "USD"
    if "$" in a:
        return a.replace("$", "€"), "currency $ -> €", "$"
    return None


def _next_invoice(inv: str) -> str:
    return f"INV-{int(inv[4:]) + 1:04d}"


def wrong_invoice_id(item: Mapping[str, Any], rng: random.Random) -> Mutation | None:
    ids = sorted(set(INVOICE.findall(item["answer"])))
    if not ids:
        return None
    inv = rng.choice(ids)
    new = _next_invoice(inv)
    return re.sub(rf"\b{inv}\b", new, item["answer"]), f"invoice {inv} -> {new}", inv


def wrong_recipient(item: Mapping[str, Any], rng: random.Random) -> Mutation | None:
    a = item["answer"]
    present = [
        (kind, value) for name, email in CUSTOMERS for kind, value in (("name", name), ("email", email)) if value in a
    ]
    if not present:
        return None
    kind, old = rng.choice(present)
    others = [c for c in CUSTOMERS if old not in c]
    replacement = rng.choice(others)[0 if kind == "name" else 1]
    return a.replace(old, replacement), f"recipient {kind} {old} -> {replacement}", old


def _failed_call(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return next((s for s in item["steps"] if s["kind"] == "tool_call" and s.get("error")), None)


def _request_invoice(item: Mapping[str, Any]) -> str:
    found = INVOICE.findall(item["input"])
    return found[0] if found else "the invoice"


def success_after_failure(item: Mapping[str, Any], rng: random.Random) -> Mutation | None:
    failed = _failed_call(item)
    if failed is None:
        return None
    inv = _request_invoice(item)
    tool = failed["name"]
    if tool == "void_invoice":
        claim = f"{inv} has been voided."
    elif tool == "send_payment_reminder":
        claim = f"Payment reminder sent to {failed['args'].get('email', 'the customer')} for {inv}."
    elif tool == "get_payment_history":
        claim = f"{inv} has received 2 payment(s) totalling 350.00 USD."
    elif tool == "get_invoice" and "reminder" in item["input"].lower():
        claim = f"{inv} is already paid, so no reminder was sent."
    elif tool == "get_invoice":
        claim = f"{inv} is paid: 480.00 USD."
    else:
        return None
    return claim, f"{tool} failed; the answer claims its result instead", item["answer"]


def unperformed_action(item: Mapping[str, Any], rng: random.Random) -> Mutation | None:
    done = {s["name"] for s in item["steps"] if s["kind"] == "tool_call" and not s.get("error")}
    inv = _request_invoice(item)
    if inv == "the invoice":
        return None
    claims = {
        "send_payment_reminder": f"I also sent a payment reminder for {inv}.",
        "issue_refund": f"I also issued a refund on {inv}.",
        "void_invoice": f"I also voided {inv}.",
    }
    tool = rng.choice([t for t in WRITE_TOOLS if t not in done])
    return f"{item['answer'].rstrip()} {claims[tool]}", f"claims {tool}, which was never called", ""


def dropped_negation(item: Mapping[str, Any], rng: random.Random) -> Mutation | None:
    for pattern, positive in NEGATIONS:
        m = re.search(pattern, item["answer"])
        if m:
            a = item["answer"]
            return (
                a[: m.start()] + positive + a[m.end() :],
                f"negation dropped: {m.group(0)!r} -> {positive!r}",
                m.group(0),
            )
    return None


# graceful_tool_failure's rubric judges only the failure report (a claimed result the failure prevented,
# retries), so it gets just these two; a wrong id or an unrelated added action is outside what it labels.
GRACEFUL_MUTATIONS = frozenset({"success_after_failure", "dropped_negation"})

RULES: dict[str, Callable[[Mapping[str, Any], random.Random], Mutation | None]] = {
    "wrong_amount": wrong_amount,
    "wrong_currency": wrong_currency,
    "wrong_invoice_id": wrong_invoice_id,
    "wrong_recipient": wrong_recipient,
    "success_after_failure": success_after_failure,
    "unperformed_action": unperformed_action,
    "dropped_negation": dropped_negation,
}


def _distinct(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One item per (check, answer): duplicated texts in the held-out set would give duplicate mutants."""
    seen: set[tuple[str, str]] = set()
    out = []
    for item in sorted(items, key=lambda i: i["id"]):
        key = (item["check"], item["answer"])
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _touches_check(item: Mapping[str, Any], old: str, new: str) -> bool:
    """Whether the changed text (before or after the mutation) overlaps what the check's params name: a
    phrase of answer_contains, the pattern of answer_regex, or a forbidden claim of graceful_tool_failure."""
    params = item.get("params") or {}
    safety = item.get("safety") or {}
    targets = [
        t.lower()
        for t in [*params.get("phrases", []), params.get("pattern", ""), *safety.get("forbidden_claims", [])]
        if t
    ]
    spans = [s.lower().strip("$€ ") for s in (old, new)]
    return any(t in s or s in t for t in targets for s in spans if s)


def generate(
    source: list[dict[str, Any]],
    seed: int = SEED,
    exclude: frozenset[tuple[str, str]] = frozenset(),
    prefix: str = "mut",
) -> list[dict[str, Any]]:
    """`exclude`: (check, answer) pairs that must not be produced (e.g. an earlier mutant set's)."""
    rng = random.Random(seed)
    pool = _distinct(source)
    out: list[dict[str, Any]] = []
    used: set[tuple[str, str]] = set(exclude)
    for mutation, quota in QUOTA.items():
        rule = RULES[mutation]
        candidates = [
            i
            for i in pool
            if (i["check"] != "graceful_tool_failure" or mutation in GRACEFUL_MUTATIONS)
            and rule(i, random.Random(0)) is not None
        ]
        made = 0
        for item in rng.sample(candidates, len(candidates)):
            if made == quota:
                break
            result = rule(item, rng)
            assert result is not None
            answer, what, old = result
            if answer == item["answer"] or (item["check"], answer) in used:
                continue
            used.add((item["check"], answer))
            made += 1
            # Text the mutation introduces that may itself be what the check names: a replacing claim or an
            # added sentence (a substituted token is judged by what it replaced).
            new = {"success_after_failure": answer, "unperformed_action": answer[len(item["answer"].rstrip()) :]}.get(
                mutation, ""
            )
            mutant = deepcopy(item)
            mutant.update(
                id=f"{prefix}-{len(out) + 1:03d}", label="incorrect", reason=f"{mutation}: {what}", answer=answer
            )
            mutant["mutation"] = {
                "type": mutation,
                "from": item["id"],
                "changed": what,
                "touches_check": _touches_check(item, old, new),
            }
            out.append(mutant)
    return out


HEADER = """\
# MUTATION calibration set for the answer checks (answer_contains, answer_regex) and graceful_tool_failure.
#
# Generated by `python -m agentforge_cli.mutants` (cli/agentforge_cli/mutants.py, seed 7) from the held-out set
# answer_checks_holdout_v1.yaml, whose answers are all labeled correct. Each mutant breaks exactly one fact
# of one held-out answer (one distinct answer per (check, text)); its steps, params and safety expectations
# are the original's, so the recorded trajectory contradicts the mutant. Every mutant is labeled incorrect by
# construction. `mutation.touches_check` says whether the broken fact is the one the check's params name
# (e.g. a wrong amount in an answer whose check only asks for "voided" is a false claim the check isn't
# configured to look for). Don't edit by hand: regenerate. A test pins the labels' hash and regenerates the
# file byte-for-byte.
"""


HOLDOUT_SEED = 11
DEV_SOURCE = REPO / "datasets" / "calibration" / "answer_checks_v1.yaml"
HOLDOUT_TARGET = REPO / "datasets" / "calibration" / "answer_checks_mutants_holdout_v1.yaml"

HOLDOUT_HEADER = """\
# FRESH HELD-OUT MUTATION set (Benchmarks part D), written before answer_grounded@1.0.0 existed.
#
# Generated by `python -m agentforge_cli.mutants --holdout` (cli/agentforge_cli/mutants.py, seed 11, the same
# mutation rules and quotas as answer_checks_mutants_v1.yaml) from every answer labeled correct in the dev set
# (answer_checks_v1.yaml) and the held-out set (answer_checks_holdout_v1.yaml). No (check, answer) of
# answer_checks_mutants_v1.yaml is produced again. Each mutant breaks exactly one fact; steps, params and
# safety expectations are the original's; every mutant is labeled incorrect by construction.
# answer_checks_mutants_v1.yaml is answer_grounded's dev set; THIS file is never used to change it. Don't
# edit by hand: regenerate. A test pins the labels' hash and regenerates the file byte-for-byte.
"""


def render(
    mutants: list[dict[str, Any]],
    header: str = HEADER,
    name: str = "answer-checks-calibration-mutants",
    seed: int = SEED,
) -> str:
    body = yaml.safe_dump(
        {"name": name, "version": 1, "seed": seed, "items": mutants},
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    return header + body


def holdout_mutants() -> list[dict[str, Any]]:
    """The fresh held-out mutants: dev + held-out correct answers, seed 11, excluding the dev mutants' texts."""
    correct = [
        i
        for path in (DEV_SOURCE, SOURCE)
        for i in yaml.safe_load(path.read_text(encoding="utf-8"))["items"]
        if i["label"] == "correct"
    ]
    earlier = yaml.safe_load(TARGET.read_text(encoding="utf-8"))["items"]
    exclude = frozenset((m["check"], m["answer"]) for m in earlier)
    return generate(correct, seed=HOLDOUT_SEED, exclude=exclude, prefix="hmut")


def render_holdout() -> str:
    return render(holdout_mutants(), HOLDOUT_HEADER, "answer-checks-calibration-mutants-holdout", HOLDOUT_SEED)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "--holdout" in args:
        HOLDOUT_TARGET.write_text(render_holdout(), encoding="utf-8", newline="\n")
        print(f"wrote {HOLDOUT_TARGET.relative_to(REPO)}")
        return
    source = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))["items"]
    TARGET.write_text(render(generate(source)), encoding="utf-8", newline="\n")
    print(f"wrote {TARGET.relative_to(REPO)}")


if __name__ == "__main__":
    main()
