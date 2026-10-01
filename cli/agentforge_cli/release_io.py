"""agentforge.yaml loading (validated on load) and release-gate rendering.

    api_url: http://127.0.0.1:8000        # optional
    release_policy:                       # see agentforge_evaluators.release
      minimums: {pass_rate: 0.9}
      maximums: {p95_latency_ms: 500}
      regressions: {pass_rate: {max_drop: 0.02}}
      cases: {no_newly_failing_tags: [critical]}

The policy is validated here (offline, same parser as the API), so a typo
fails before anything is sent; the API validates it again and computes every
check itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from agentforge_evaluators import Policy, PolicyError, parse_policy

CONFIG_KEYS = {"api_url", "release_policy"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProjectConfig:
    api_url: str | None
    policy: Policy | None


def load_config(path: Path) -> ProjectConfig:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {unknown} (accepted: {sorted(CONFIG_KEYS)})")
    api_url = raw.get("api_url")
    if api_url is not None and not isinstance(api_url, str):
        raise ConfigError(f"{path}: api_url must be a string")
    policy = None
    if "release_policy" in raw:
        try:
            policy = parse_policy(raw["release_policy"])
        except PolicyError as exc:
            raise ConfigError(f"{path}: release_policy: {exc}") from exc
    return ProjectConfig(api_url=api_url, policy=policy)


# -- rendering ------------------------------------------------------------------------


def fmt_number(value: float | None, metric: str = "") -> str:
    if value is None:
        return "-"
    if metric.endswith("_ms") or metric.endswith(".mean_value") and "latency" in metric:
        return f"{value:.1f}"
    if float(value).is_integer() and abs(value) >= 1:
        return f"{value:.0f}"
    return f"{value:.4g}"


def fmt_delta(check: dict[str, Any]) -> str:
    d = check.get("delta")
    if d is None:
        return "-"
    pct = check.get("delta_pct")
    text = f"{d:+.4g}"
    return f"{text} ({pct:+.1f}%)" if pct is not None else text


def verdict_line(passed: bool) -> str:
    return f"RELEASE GATE: {'PASSED' if passed else 'FAILED'}"


def markdown_report(decision: dict[str, Any]) -> str:
    """The decision as GitHub-flavored Markdown (for $GITHUB_STEP_SUMMARY)."""
    passed = decision["passed"]
    failed = sum(1 for c in decision["checks"] if not c["passed"])
    lines = [
        f"## AgentForge release gate: {'PASSED' if passed else 'FAILED'}",
        "",
        f"**{decision['application_name']}** — candidate run `{decision['candidate_run_id']}` vs baseline "
        f"`{decision['baseline_ref']}` (run `{decision['baseline_run_id']}`). "
        f"{len(decision['checks']) - failed} of {len(decision['checks'])} checks passed. "
        f"Decision `{decision['id']}`.",
        "",
        "| Result | Check | Metric | Baseline | Candidate | Delta | Threshold | Reason |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in decision["checks"]:
        lines.append(
            f"| {'pass' if c['passed'] else '**FAIL**'} | {c['kind']} | `{c['metric']}` | "
            f"{fmt_number(c['baseline'], c['metric'])} | {fmt_number(c['candidate'], c['metric'])} | "
            f"{fmt_delta(c)} | {c['rule']} | {c['reason'].replace('|', '/')} |"
        )
    counts = decision["regression"]["case_counts"]
    lines += [
        "",
        "Cases: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()) + ".",
    ]
    newly = decision["regression"]["cases"]["newly_failing"]
    if newly:
        lines.append("")
        lines.append("Newly failing:")
        for case in newly:
            tags = f" [{', '.join(case['tags'])}]" if case["tags"] else ""
            failed_by = ", ".join(case["candidate_failed_evaluators"]) or case["candidate_status"]
            lines.append(f"- `{case['case_key']}`{tags}: {failed_by}")
    lines += ["", "All checks are arithmetic over persisted run aggregates; none is an LLM judgment.", ""]
    lines.append(f"**{verdict_line(passed)}**")
    # Trailing blank line: several gates may append to the same step summary.
    return "\n".join(lines) + "\n\n"
