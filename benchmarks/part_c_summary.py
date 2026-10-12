"""Benchmarks part C summary: writes benchmarks/2026-10-12-part-c.json from stored data only.

    python benchmarks/part_c_summary.py [--api-url http://127.0.0.1:8000]

* `rescored`: every Part B run (benchmarks/2026-10-08-compare-a365a887.json) next to its re-score with the
  @1.1.0 answer checks (the newest re-score of that run whose evaluators include all three), with the same
  metrics `agentforge compare` reports (agentforge_cli.benchmark.run_metrics), from the API.
* `calibration`: agreement of @1.0.0 and @1.1.0 with the three calibration sets (dev, held-out, mutants),
  computed now by agentforge_cli.calibration (the evaluators run on the stored items; nothing is called).
* `reruns`: the part C compare runs, read from their results JSON (estimate vs actual spend).

A different `schema` id than the compare results, so the cost estimator doesn't read it as a run.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import yaml

from agentforge_cli import benchmark, calibration
from agentforge_evaluators import parse_pricing

ROOT = Path(__file__).resolve().parents[1]
PART_B = ROOT / "benchmarks" / "2026-10-08-compare-a365a887.json"
RERUNS = [
    ("approval-enum case (amount-in-words)", "2026-10-12-compare-016be482.json"),
    ("the four other approval-action cases", "2026-10-12-compare-23bc471c.json"),
    ("Llama 3.1 8B with the tools-every-turn template", "2026-10-12-compare-0a9381f6.json"),
]
NEW = ("answer_contains@1.1.0", "answer_regex@1.1.0", "graceful_tool_failure@1.1.0")
SETS = {
    "dev": "answer_checks_v1.yaml",
    "holdout": "answer_checks_holdout_v1.yaml",
    "mutants": "answer_checks_mutants_v1.yaml",
}


def _summary(values: list[float | None]) -> dict[str, float | int | None]:
    present = [v for v in values if v is not None]
    if not present:
        return {"mean": None, "min": None, "max": None, "n": 0}
    return {"mean": statistics.fmean(present), "min": min(present), "max": max(present), "n": len(present)}


def rescored(client: httpx.Client) -> list[dict[str, Any]]:
    pricing = parse_pricing(yaml.safe_load((ROOT / "config" / "pricing.yaml").read_text(encoding="utf-8")))
    part_b = json.loads(PART_B.read_text(encoding="utf-8"))
    rows = []
    for row in part_b["results"]:
        before: dict[str, list[float | None]] = {}
        after: dict[str, list[float | None]] = {}
        runs = []
        for run in row["runs"]:
            candidates = [
                r
                for r in client.get(f"/runs/{run['run_id']}/rescores").json()
                if r["status"] == "completed" and set(NEW) <= set(r["evaluators"])
            ]
            if not candidates:
                raise SystemExit(f"run {run['run_id']} has no completed @1.1.0 re-score")
            rescore = client.get(f"/rescores/{candidates[0]['id']}").json()
            original = client.get(f"/runs/{run['run_id']}").json()
            for target, source in ((before, original), (after, rescore)):
                for k, v in benchmark.run_metrics(source, pricing).items():
                    target.setdefault(k, []).append(v)
            runs.append(
                {
                    "run_id": run["run_id"],
                    "rescore_id": rescore["id"],
                    "newly_passing": rescore["newly_passing"],
                    "newly_failing": rescore["newly_failing"],
                }
            )
        rows.append(
            {
                "model": row["model"],
                "dataset": row["dataset"],
                "runs": runs,
                "at_1_0_0": {k: _summary(v) for k, v in before.items()},
                "at_1_1_0": {k: _summary(v) for k, v in after.items()},
            }
        )
    return rows


def calibration_results() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, file in SETS.items():
        items = calibration.load(ROOT / "datasets" / "calibration" / file)
        per_version = {}
        for version in ("1.0.0", "1.1.0"):
            t = calibration.total(calibration.agreement(items, version), version)
            per_version[version] = {
                "items": t.items,
                "agree": t.agree,
                "accuracy": t.accuracy,
                "false_positives": t.false_positives,
                "false_negatives": t.false_negatives,
            }
        entry: dict[str, Any] = {
            "file": f"datasets/calibration/{file}",
            "labels": dict(Counter(i["label"] for i in items)),
            "versions": per_version,
        }
        if name == "mutants":
            by_type: dict[str, dict[str, int]] = {}
            for item in items:
                t = by_type.setdefault(item["mutation"]["type"], {"n": 0, "caught_1_0_0": 0, "caught_1_1_0": 0})
                t["n"] += 1
                t["caught_1_0_0"] += calibration.verdict(item, "1.0.0") is False
                t["caught_1_1_0"] += calibration.verdict(item, "1.1.0") is False
            entry["by_mutation_type"] = by_type
        out[name] = entry
    return out


def reruns() -> list[dict[str, Any]]:
    out = []
    for what, file in RERUNS:
        path = ROOT / "benchmarks" / file
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            {
                "what": what,
                "file": f"benchmarks/{file}",
                "estimate": doc["estimate"]["per_model"],
                "estimate_total_usd": doc["estimate"]["total_usd"],
                "actual_spend_usd": doc["actual_spend_usd"],
                "code_versions": doc["agentforge"]["code_versions"],
                "pass_rate": {f"{r['model']} / {r['dataset']}": r["summary"]["pass_rate"] for r in doc["results"]},
            }
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks" / "2026-10-12-part-c.json")
    args = parser.parse_args()
    with httpx.Client(base_url=args.api_url, timeout=60) as client:
        doc = {
            "schema": "agentforge.benchmark-part-c/v1",
            "date": "2026-10-12",
            "about": (
                "Benchmarks part C: the Part B runs re-scored with the @1.1.0 answer checks (no model called), the "
                "evaluator calibration results, and the part C re-runs. Cost is estimated (reported tokens x "
                "config/pricing.yaml). Generated by benchmarks/part_c_summary.py from stored runs and re-scores."
            ),
            "part_b_results": "benchmarks/2026-10-08-compare-a365a887.json",
            "evaluators": {"at_1_0_0": "as the Part B runs pinned them", "at_1_1_0": list(NEW)},
            "rescored": rescored(client),
            "calibration": calibration_results(),
            "reruns": reruns(),
        }
    args.output.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
