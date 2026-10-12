"""Benchmarks part D summary: writes benchmarks/2026-10-12-part-d.json from stored data only.

    python benchmarks/part_d_summary.py [--api-url http://127.0.0.1:8000]

* `calibration`: answer_grounded@1.0.0 and @1.1.0 on the four calibration sets (dev 45, held-out 25, dev
  mutants 30, fresh held-out mutants 27), computed now (the evaluator runs on the stored items). 1.0.0's
  held-out numbers are held-out; every 1.1.0 number is in-sample (it was written after seeing them).
* `rescored`: every benchmark run (Part B and the part C re-runs) re-scored with each answer_grounded
  version (the newest re-score of that run that pins it), from the API: pass rate, the cases answer_grounded
  failed with its reason, and whether the case verdict changed against the run's re-score without it.

Opt-in: answer_grounded is in no default evaluator set, so no run or gate result here changes by default.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from agentforge_cli import calibration
from agentforge_evaluators import EvalConfig, resolve

ROOT = Path(__file__).resolve().parents[1]
RESULTS = [
    "2026-10-08-compare-a365a887.json",
    "2026-10-12-compare-016be482.json",
    "2026-10-12-compare-23bc471c.json",
    "2026-10-12-compare-0a9381f6.json",
]
SETS = {
    "dev": "answer_checks_v1.yaml",
    "holdout": "answer_checks_holdout_v1.yaml",
    "mutants_dev": "answer_checks_mutants_v1.yaml",
    "mutants_holdout": "answer_checks_mutants_holdout_v1.yaml",
}
VERSIONS = ("1.0.0", "1.1.0")


def calibration_results() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for version in VERSIONS:
        fn = resolve(f"answer_grounded@{version}").fn
        per_set: dict[str, Any] = {}
        for name, file in SETS.items():
            items = calibration.load(ROOT / "datasets" / "calibration" / file)
            flagged = {i["id"] for i in items if fn(calibration.eval_input(i), EvalConfig(), {}).passed is False}
            entry: dict[str, Any] = {
                "items": len(items),
                "labels": dict(Counter(i["label"] for i in items)),
                "false_positives": sorted(i["id"] for i in items if i["label"] == "correct" and i["id"] in flagged),
                "incorrect_flagged": sorted(i["id"] for i in items if i["label"] == "incorrect" and i["id"] in flagged),
            }
            if name.startswith("mutants"):
                by_type: dict[str, dict[str, int]] = {}
                for item in items:
                    t = by_type.setdefault(item["mutation"]["type"], {"n": 0, "caught": 0})
                    t["n"] += 1
                    t["caught"] += item["id"] in flagged
                entry["by_mutation_type"] = by_type
            per_set[name] = entry
        out[version] = {
            "in_sample": version == "1.1.0",
            "note": (
                "dev sets in-sample (written against them); held-out sets run once after the evaluator was committed"
                if version == "1.0.0"
                else "every number in-sample: written after 1.0.0's held-out and benchmark results were seen"
            ),
            "sets": per_set,
        }
    return out


def _latest(client: httpx.Client, run_id: str, version: str) -> dict[str, Any] | None:
    for summary in client.get(f"/runs/{run_id}/rescores").json():  # newest first
        if summary["status"] == "completed" and f"answer_grounded@{version}" in summary["evaluators"]:
            rescore: dict[str, Any] = client.get(f"/rescores/{summary['id']}").json()
            return rescore
    return None


def _without_grounding(client: httpx.Client, run_id: str) -> dict[str, bool]:
    """case_key -> verdict with the @1.1.0 answer checks but no answer_grounded: the Part C re-score for Part B
    runs, the run itself for the part C re-runs (they already pinned @1.1.0)."""
    for summary in client.get(f"/runs/{run_id}/rescores").json():
        if summary["status"] == "completed" and not any(e.startswith("answer_grounded") for e in summary["evaluators"]):
            return {r["case_key"]: r["passed"] for r in client.get(f"/rescores/{summary['id']}").json()["results"]}
    run = client.get(f"/runs/{run_id}").json()
    return {r["case_key"]: bool(r["passed"]) for r in run["results"]}


def rescored(client: httpx.Client) -> list[dict[str, Any]]:
    rows = []
    for file in RESULTS:
        doc = json.loads((ROOT / "benchmarks" / file).read_text(encoding="utf-8"))
        for row in doc["results"]:
            for run in row["runs"]:
                base = _without_grounding(client, run["run_id"])
                entry: dict[str, Any] = {
                    "results_file": f"benchmarks/{file}",
                    "model": row["model"],
                    "dataset": row["dataset"],
                    "run_id": run["run_id"],
                    "pass_rate_without_grounding": sum(base.values()) / len(base),
                }
                for version in VERSIONS:
                    rescore = _latest(client, run["run_id"], version)
                    if rescore is None:
                        raise SystemExit(f"run {run['run_id']} has no completed answer_grounded@{version} re-score")
                    failed = []
                    for r in rescore["results"]:
                        m = next((m for m in r["metrics"] if m["evaluator_name"] == "answer_grounded"), None)
                        if m is not None and m["passed"] is False:
                            failed.append(
                                {
                                    "case_key": r["case_key"],
                                    "reason": m["reason"],
                                    "verdict_changed": base[r["case_key"]] != r["passed"],
                                }
                            )
                    entry[f"answer_grounded@{version}"] = {
                        "rescore_id": rescore["id"],
                        "pass_rate": rescore["aggregates"]["pass_rate"],
                        "flagged": failed,
                    }
                rows.append(entry)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks" / "2026-10-12-part-d.json")
    args = parser.parse_args()
    with httpx.Client(base_url=args.api_url, timeout=60) as client:
        doc = {
            "schema": "agentforge.benchmark-part-d/v1",
            "date": "2026-10-12",
            "about": (
                "Benchmarks part D: answer_grounded (opt-in) calibration and the benchmark runs re-scored with it "
                "(no model called). Generated by benchmarks/part_d_summary.py from the calibration sets and stored "
                "re-scores."
            ),
            "calibration": calibration_results(),
            "rescored": rescored(client),
        }
    args.output.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
