"""`agentforge replay`: building the overrides from the command line, and
printing a replay next to the original as a before/after diff.

The CLI doesn't know which settings an adapter accepts (that's declared in
the adapter, inside the worker image), so it only turns the flags into an
overrides object; the worker validates it and the replay fails with the
adapter's message if it doesn't match.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text


class OverrideArgsError(ValueError):
    pass


def parse_value(raw: str) -> Any:
    """`--set key=value`: JSON if the value is JSON (3, true, "x", {...}), else the string itself."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def build_overrides(sets: list[str], prompt_file: Path | None, retrieval_config: Path | None) -> dict[str, Any]:
    overrides: dict[str, Any] = {}

    def put(key: str, value: Any, source: str) -> None:
        if key in overrides:
            raise OverrideArgsError(f"'{key}' is set more than once ({source})")
        overrides[key] = value

    for item in sets:
        key, sep, raw = item.partition("=")
        key = key.strip()
        if not sep or not key:
            raise OverrideArgsError(f"--set expects key=value, got {item!r}")
        put(key, parse_value(raw), f"--set {item}")
    if prompt_file is not None:
        try:
            put("prompt", prompt_file.read_text(encoding="utf-8"), "--prompt-file")
        except OSError as exc:
            raise OverrideArgsError(f"--prompt-file: {exc}") from exc
    if retrieval_config is not None:
        try:
            config = yaml.safe_load(retrieval_config.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise OverrideArgsError(f"--retrieval-config: {exc}") from exc
        if not isinstance(config, dict):
            raise OverrideArgsError("--retrieval-config must contain a YAML/JSON object (mapping)")
        put("retrieval_config", config, "--retrieval-config")
    return overrides


def _verdict(passed: bool | None) -> str:
    return "-" if passed is None else ("PASS" if passed else "FAIL")


def _metric(m: dict[str, Any] | None) -> str:
    if m is None:
        return "-"
    if m["score"] is not None:
        shown = f"{m['score']:.2f}"
    elif m["value"] is not None:
        shown = f"{m['value']:.2f} ms" if m["unit"] == "ms" else f"{m['value']:.6g}{m['unit'] or ''}"
    else:
        shown = ""
    verdict = "n/a" if m["passed"] is None and not shown else ("" if m["passed"] is None else _verdict(m["passed"]))
    return " ".join(part for part in (verdict, shown) if part)


def _not_applicable(ev: dict[str, Any]) -> bool:
    """Unchanged and not applicable on both sides (no verdict, nothing measured)."""
    return ev["change"] == "unchanged" and all(
        m is not None and m["passed"] is None and m["score"] is None and m["value"] is None
        for m in (ev["before"], ev["after"])
    )


def _compact(value: Any, limit: int = 90) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _step_label(step: dict[str, Any]) -> str:
    if step["kind"] == "tool_call":
        return f"{step['name']} {_compact(step['args'])}"
    if step["kind"] == "retrieval":
        return f"retrieval {_compact(step['retrieved_doc_ids'])}"
    return "final_answer"


def _number(change: dict[str, Any], unit: str) -> str:
    before, after, delta = change["before"], change["after"], change["delta"]
    if before is None and after is None:
        return "not reported"
    spec = ".2f" if unit == " ms" else ".6g"
    b = "-" if before is None else f"{before:{spec}}{unit}"
    a = "-" if after is None else f"{after:{spec}}{unit}"
    return f"{b} -> {a}" + (f" ({delta:+{spec}}{unit})" if delta is not None else "")


_STYLE = {"unchanged": "", "changed": "yellow", "added": "green", "removed": "red"}
_EVAL_STYLE = {"unchanged": "", "fixed": "green", "regressed": "red", "changed": "yellow", "added": "", "removed": ""}


def print_replay(console: Console, replay: dict[str, Any]) -> None:
    labels = f" [{', '.join(replay['labels'])}]" if replay["labels"] else ""
    console.print(
        f"\nReplay {replay['id']} of [bold]{escape(replay['case_key'])}[/bold] - "
        f"[bold]{replay['status']}[/bold]{escape(labels)}"
    )
    console.print(
        f"original: result {replay['original_result_id']} in run {replay['original_run_id']}, "
        f"adapter {escape(replay['adapter_type'])}:{escape(replay['adapter_target'])}"
    )
    overrides = replay["overrides"]
    shown = ", ".join(f"{k}={_compact(v, 60)}" for k, v in sorted(overrides.items())) if overrides else "none"
    console.print(f"overrides: {escape(shown)}")
    if replay.get("dataset_content_hash"):
        console.print(f"dataset content hash: {replay['dataset_content_hash']} (same dataset version as the original)")
    if replay["error_message"]:
        console.print(f"[red]Replay failed:[/red] {escape(replay['error_message'])}")
    diff = replay.get("diff")
    if diff is None:
        return

    console.print(
        f"\nCase: {_verdict(diff['before_passed'])} -> [bold]{_verdict(diff['after_passed'])}[/bold]"
        f"   (status {diff['before_status']} -> {diff['after_status']})"
    )
    if diff["identical"]:
        console.print("[green]Identical to the original[/green]: every step, tool call, the answer and every verdict.")
    else:
        console.print(f"Differs from the original in {len(diff['differences'])} place(s).")

    table = Table(title="Evaluators (original -> replay)")
    table.add_column("Evaluator")
    table.add_column("Before")
    table.add_column("After")
    table.add_column("Change")
    table.add_column("Reason (after)")
    not_applicable = [ev["evaluator_name"] for ev in diff["evaluators"] if _not_applicable(ev)]
    for ev in diff["evaluators"]:
        if ev["evaluator_name"] in not_applicable:
            continue
        style = _EVAL_STYLE[ev["change"]]
        after = ev["after"] or {}
        table.add_row(
            escape(ev["evaluator_name"]),
            _metric(ev["before"]),
            _metric(ev["after"]),
            Text(ev["change"], style=style),
            escape(after.get("reason", "")) if ev["change"] != "unchanged" else "",
        )
    console.print(table)
    if not_applicable:
        console.print(f"Not applicable to this case, before and after: {escape(', '.join(not_applicable))}")

    steps = Table(title="Trajectory (original -> replay)")
    steps.add_column("Before", justify="right")
    steps.add_column("After", justify="right")
    steps.add_column("Change")
    steps.add_column("Step")
    for s in diff["trajectory"]:
        before, after = s["before"], s["after"]
        label = _step_label(after or before)
        if s["op"] == "changed":
            parts = [f"{f}: {_compact(before[f], 40)} -> {_compact(after[f], 40)}" for f in s["changed_fields"]]
            label = f"{s['name'] or s['kind']}  " + "; ".join(parts)
        steps.add_row(
            str(before["step_index"]) if before else "-",
            str(after["step_index"]) if after else "-",
            Text(s["op"], style=_STYLE[s["op"]]),
            Text(label, style=_STYLE[s["op"]]),
        )
    console.print(steps)

    answer = diff["answer"]
    console.print("Final answer:" + ("" if answer["changed"] else " unchanged"))
    if answer["changed"]:
        before = Text("  - ")
        after = Text("  + ")
        for seg in answer["segments"]:
            if seg["op"] in ("equal", "delete"):
                before.append(seg["text"], style="red strike" if seg["op"] == "delete" else "")
            if seg["op"] in ("equal", "insert"):
                after.append(seg["text"], style="green" if seg["op"] == "insert" else "")
        console.print(before)
        console.print(after)
    else:
        console.print(Text(f"  {answer['after'] or ''}"))
    console.print(
        f"Latency: {_number(diff['latency_ms'], ' ms')}   "
        f"Tokens in: {_number(diff['input_tokens'], '')}   out: {_number(diff['output_tokens'], '')}"
    )
    if "fixture-based" in replay["labels"]:
        console.print(
            "fixture-based: the local-deterministic provider (synthetic agent, deterministic evaluators). "
            "Not model quality, and not an LLM judgment."
        )
