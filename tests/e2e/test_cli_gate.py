"""End-to-end release gate through the real CLI (subprocess) -> a real uvicorn
API -> Redis -> the Docker worker -> Postgres: evaluate the invoice agent's
v1 twice and v2 once, set the production baseline, and gate. Checks exit
codes (0 PASSED / 1 FAILED / 2 couldn't evaluate), the final verdict line,
and the Markdown written to $GITHUB_STEP_SUMMARY.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
from test_cli_evaluate import REPO_ROOT, live_api, worker_api  # noqa: F401 -- fixtures


def _cli(args: list[str], **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentforge_cli.main", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "COLUMNS": "200", **env},
    )


def _evaluate(api: str, version: str) -> str:
    before = {r["id"] for r in httpx.get(f"{api}/runs", timeout=5).json()}
    done = _cli(
        [
            "evaluate", "--app", "invoice-agent", "--app-version", version, "--dataset", "invoice-agent",
            "--adapter", f"invoice_agent.adapter:answer_{version}", "--api-url", api,
            "--poll-interval", "0.5", "--wait-timeout", "120",
        ]
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    [new] = [r["id"] for r in httpx.get(f"{api}/runs", timeout=5).json() if r["id"] not in before]
    return new


def test_cli_gate_passes_v1_and_fails_v2(worker_api: str, tmp_path: Path) -> None:  # noqa: F811
    publish = _cli(["dataset", "publish", "datasets/invoice_agent_v1.yaml", "--api-url", worker_api])
    assert publish.returncode == 0, publish.stdout + publish.stderr
    baseline, v1_candidate, v2_candidate = (_evaluate(worker_api, v) for v in ("v1", "v1", "v2"))

    set_baseline = _cli(["baseline", "set", baseline, "--env", "production", "--api-url", worker_api])
    assert set_baseline.returncode == 0, set_baseline.stdout + set_baseline.stderr
    assert f"invoice-agent / production -> run {baseline}" in set_baseline.stdout
    show = _cli(["baseline", "show", "--api-url", worker_api])
    assert "production" in show.stdout and baseline in show.stdout

    compare = _cli(["compare", "--baseline", baseline, "--candidate", v2_candidate, "--api-url", worker_api])
    assert compare.returncode == 0, compare.stdout + compare.stderr
    assert "newly failing 6" in compare.stdout
    assert "newly failing: refund-over-limit-001 [refund, approval, guard, critical]" in compare.stdout

    summary = tmp_path / "step_summary.md"
    gate_args = ["gate", "--baseline", "production", "--api-url", worker_api]
    passed = _cli([*gate_args, "--candidate", v1_candidate], GITHUB_STEP_SUMMARY=str(summary))
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert passed.stdout.rstrip().splitlines()[-1] == "RELEASE GATE: PASSED"
    assert summary.read_text(encoding="utf-8").startswith("## AgentForge release gate: PASSED")

    failed = _cli([*gate_args, "--candidate", v2_candidate], GITHUB_STEP_SUMMARY=str(summary))
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert failed.stdout.rstrip().splitlines()[-1] == "RELEASE GATE: FAILED"
    # Bracketed values survive Rich's markup parsing.
    assert "newly_failing[tag=critical]" in failed.stdout
    assert "refund-small-001 [refund, approval, write, critical]" in failed.stdout
    report = summary.read_text(encoding="utf-8")
    assert "## AgentForge release gate: FAILED" in report  # appended after the PASSED one
    assert "| **FAIL** | minimum | `pass_rate` | 1 | 0.4 |" in report

    # Both decisions were recorded by the API.
    decisions = httpx.get(f"{worker_api}/release-decisions", timeout=5).json()
    assert sorted(d["passed"] for d in decisions) == [False, True]


def test_cli_gate_exits_2_when_it_cannot_evaluate(live_api: str, tmp_path: Path) -> None:  # noqa: F811
    bad = tmp_path / "agentforge.yaml"
    bad.write_text("release_policy:\n  minimums: {p95_latency_ms: 100}\n", encoding="utf-8")
    result = _cli(["gate", "--candidate", "x", "--baseline", "production", "--config", str(bad), "--api-url", live_api])
    assert result.returncode == 2
    assert "Invalid config" in result.stdout and "lower-is-better" in result.stdout

    # Valid config, but the candidate run doesn't exist: the API's 404, still exit 2.
    result = _cli(["gate", "--candidate", "missing", "--baseline", "production", "--api-url", live_api])
    assert result.returncode == 2
    assert "candidate run 'missing' not found" in result.stdout
