"""`agentforge rescore`: printing a re-score next to the run it re-scored, as a before/after table.

The re-score itself carries the new verdicts; the original run (fetched alongside) carries the old
ones. Only the evaluators whose version changed can differ, so those are the columns shown.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text


def split_evaluators(values: list[str]) -> list[str]:
    """`--evaluators a@1.1.0,b --evaluators c` -> [a@1.1.0, b, c] (order kept, duplicates dropped)."""
    out = [part.strip() for value in values for part in value.split(",")]
    return list(dict.fromkeys(p for p in out if p))


def _verdict(passed: bool | None) -> str:
    return "-" if passed is None else ("PASS" if passed else "FAIL")


def _metric_verdict(m: dict[str, Any] | None) -> str:
    if m is None:
        return "-"
    return "n/a" if m["passed"] is None else _verdict(m["passed"])


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _change(before: bool | None, after: bool | None) -> Text:
    if before == after:
        return Text("")
    if after and not before:
        return Text("fixed", style="green")
    if before and not after:
        return Text("broken", style="red")
    return Text("changed", style="yellow")


def changed_names(rescore: dict[str, Any]) -> list[str]:
    """Evaluator names whose version the re-score changed (or added)."""
    return sorted(rescore["replaced"])


def print_rescore(console: Console, rescore: dict[str, Any], run: dict[str, Any], show_all: bool = False) -> None:
    console.print(f"\nRe-score {rescore['id']} of run {rescore['original_run_id']} - [bold]{rescore['status']}[/bold]")
    console.print(
        f"run: {escape(run['application_name'])}@{escape(run['application_version'])} on "
        f"{escape(run['dataset_name'])} v{run['dataset_version']}; stored outputs only, no adapter was called"
    )
    names = changed_names(rescore)
    if rescore["replaced"]:
        for name in names:
            console.print(f"  {escape(name)}: {escape(rescore['replaced'][name])}")
    else:
        console.print("[yellow]No evaluator version changed[/yellow]: the run already pinned these versions.")
    if rescore.get("code_version"):
        console.print(f"scored by code {escape(str(rescore['code_version']))}")
    if rescore["error_message"]:
        console.print(f"[red]Re-score failed:[/red] {escape(rescore['error_message'])}")
    if rescore["status"] != "completed":
        return

    before_by_result = {r["id"]: r for r in run.get("results", [])}
    table = Table(title="Cases (original run -> re-score)")
    table.add_column("Case")
    table.add_column("Before")
    table.add_column("After")
    table.add_column("Change")
    for name in names:
        table.add_column(name)
    shown = 0
    for r in rescore["results"]:
        original = before_by_result.get(r["original_result_id"], {})
        old_metrics = {m["evaluator_name"]: m for m in original.get("metrics", [])}
        new_metrics = {m["evaluator_name"]: m for m in r["metrics"]}
        cells = []
        metric_changed = False
        for name in names:
            old, new = old_metrics.get(name), new_metrics.get(name)
            if old is None and new is None:
                cells.append(Text(""))
                continue
            a, b = _metric_verdict(old), _metric_verdict(new)
            metric_changed |= a != b
            cells.append(Text(f"{a} -> {b}", style="yellow" if a != b else ""))
        if not (show_all or r["changed"] or metric_changed):
            continue
        shown += 1
        table.add_row(
            escape(r["case_key"]),
            _verdict(r["original_passed"]) if r["status"] == "ok" else r["status"],
            _verdict(r["passed"]) if r["status"] == "ok" else r["status"],
            _change(r["original_passed"], r["passed"]),
            *cells,
        )
    total = len(rescore["results"])
    if shown:
        console.print(table)
    if not show_all:
        console.print(f"{total - shown} of {total} case(s) unchanged (verdict and every re-scored evaluator).")

    before, after = rescore.get("original_aggregates") or {}, rescore.get("aggregates") or {}
    console.print(
        f"\npass rate {_rate(before.get('pass_rate'))} -> [bold]{_rate(after.get('pass_rate'))}[/bold]   "
        f"newly passing {rescore['newly_passing']}, newly failing {rescore['newly_failing']}"
    )
    b_safety, a_safety = before.get("safety") or {}, after.get("safety") or {}
    if a_safety:
        cats = Table(title="Safety pass rate per category (original run -> re-score)")
        for col in ("Category", "Cases", "Before", "After"):
            cats.add_column(col)
        for category, c in a_safety["by_category"].items():
            old = (b_safety.get("by_category") or {}).get(category, {})
            cats.add_row(category, str(c["cases"]), _rate(old.get("pass_rate")), _rate(c["pass_rate"]))
        console.print(cats)
    if "fixture-based" in run.get("labels", []):
        console.print(
            "fixture-based: the local-deterministic provider (synthetic agent, deterministic evaluators). "
            "Not model quality, and not an LLM judgment."
        )
