"""AgentForge CLI: dataset publish/validate, adversarial generate, evaluate,
runs list/show, baseline set/show, compare (two runs, or several models), gate, replay.

`evaluate` does not execute anything locally: it submits a run to the API,
which queues it for the worker (Docker), then polls until the run is
completed or failed and prints the persisted results.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

# Windows consoles/pipes don't reliably default to UTF-8 (often cp1252),
# but Rich's table borders and our own output assume UTF-8. Force it so
# output is correct both in a real terminal and when captured by a test or
# CI runner, regardless of the host's console code page.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if isinstance(_stream, io.TextIOWrapper):
            try:
                _stream.reconfigure(encoding="utf-8")
            except ValueError:
                pass

import yaml
from pydantic import ValidationError

from agentforge_cli import adversarial, benchmark
from agentforge_cli.dataset_io import DatasetFileError, validate_dataset_file
from agentforge_cli.git_utils import current_commit_sha
from agentforge_cli.release_io import (
    ConfigError,
    fmt_delta,
    fmt_number,
    load_config,
    markdown_report,
    verdict_line,
)
from agentforge_cli.replay_io import OverrideArgsError, build_overrides, print_replay
from agentforge_core.schemas import FIXTURE_BASED_LABEL, LOCAL_DETERMINISTIC, AdapterSpec
from agentforge_evaluators import PricingConfigError, parse_pricing
from agentforge_sdk import AgentForgeClient

app = typer.Typer(add_completion=False, help="AgentForge command-line interface.")
dataset_app = typer.Typer(add_completion=False, help="Manage evaluation datasets.")
runs_app = typer.Typer(add_completion=False, help="Inspect evaluation runs.")
app.add_typer(dataset_app, name="dataset")
baseline_app = typer.Typer(add_completion=False, help="Baselines: (application, environment) -> run.")
adversarial_app = typer.Typer(add_completion=False, help="Adversarial (safety) test datasets.")
app.add_typer(runs_app, name="runs")
app.add_typer(baseline_app, name="baseline")
app.add_typer(adversarial_app, name="adversarial")

console = Console()

DEFAULT_API_URL = "http://127.0.0.1:8000"
FINISHED = {"completed", "failed"}
MEASUREMENT_EVALUATORS = {"latency", "token_usage", "estimated_cost"}

AGENTFORGE_YAML_TEMPLATE = """\
# AgentForge project configuration. Read by `agentforge gate` (validated on
# load: unknown keys, unknown metrics and direction mistakes are errors).

api_url: {api_url}

# Release policy for `agentforge gate --candidate <run> --baseline <env|run>`.
# Every check is arithmetic over the two runs' stored aggregates.
# release_policy:
#   minimums:                 # candidate >= value (higher-is-better metrics)
#     pass_rate: 0.9
#   maximums:                 # candidate <= value (lower-is-better metrics)
#     p95_latency_ms: 500
#   regressions:              # candidate vs baseline
#     pass_rate: {{max_drop: 0.02}}             # at most 2 points lower
#     p95_latency_ms: {{max_increase_pct: 25}}
#   cases:
#     no_newly_failing_tags: [critical]
"""


@app.command()
def init(
    api_url: str = typer.Option(DEFAULT_API_URL, help="AgentForge API base URL."),
    path: Path = typer.Option(Path("agentforge.yaml"), help="Where to write the config file."),
) -> None:
    """Scaffold an agentforge.yaml in the current directory."""
    if path.exists():
        console.print(f"[yellow]{path} already exists, not overwriting.[/yellow]")
        raise typer.Exit(code=1)
    path.write_text(AGENTFORGE_YAML_TEMPLATE.format(api_url=api_url), encoding="utf-8")
    console.print(f"[green]Wrote {path}[/green]")


@dataset_app.command("validate")
def dataset_validate(file: Path = typer.Argument(..., help="Path to a dataset YAML file.")) -> None:
    """Validate a dataset YAML file's structure offline (no API call, no DB)."""
    try:
        name, _description, test_cases, default_evaluators = validate_dataset_file(file)
    except DatasetFileError as exc:
        console.print(f"[red]Invalid dataset:[/red] {exc}")
        raise typer.Exit(code=1) from None

    console.print(f"[green]Valid[/green] dataset '{name}' with {len(test_cases)} test case(s):")
    defaults = sorted(default_evaluators or {})
    console.print(f"  default evaluators: {', '.join(defaults) if default_evaluators is not None else '(all)'}")
    for tc in test_cases:
        overrides = ", ".join(f"-{k}" if v is False else f"+{k}" for k, v in sorted((tc.evaluators or {}).items()))
        console.print(
            f"  - {tc.case_key} ({len(tc.expected_context)} expected doc id(s))"
            + (f"  evaluators: {overrides}" if overrides else "")
        )


@dataset_app.command("publish")
def dataset_publish(
    file: Path = typer.Argument(..., help="Path to a dataset YAML file."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", help="AgentForge API base URL."),
) -> None:
    """Validate, then publish a dataset YAML file as a new immutable DatasetVersion.

    Under the hood this creates a draft with these test cases and
    immediately publishes it -- from the CLI's perspective it's still one
    atomic "publish a version" operation, but it goes through the same
    draft-then-publish path the UI uses.
    """
    try:
        name, description, test_cases, default_evaluators = validate_dataset_file(file)
    except DatasetFileError as exc:
        console.print(f"[red]Invalid dataset:[/red] {exc}")
        raise typer.Exit(code=1) from None

    with AgentForgeClient(base_url=api_url) as client:
        try:
            dataset = client.upsert_dataset(name=name, description=description)
            draft = client.create_draft_version(
                dataset_id=dataset["id"],
                test_cases=[tc.model_dump() for tc in test_cases],
                default_evaluators=default_evaluators,
            )
            version = client.publish_dataset_version(dataset_name=name, version=draft["version"])
        except Exception as exc:  # noqa: BLE001 - surface any API/network error to the user
            console.print(f"[red]Failed to publish dataset:[/red] {exc}")
            raise typer.Exit(code=1) from None

    console.print(
        f"[green]Published[/green] dataset '{name}' version {version['version']} "
        f"({len(version['test_cases'])} test case(s), id={version['id']})"
    )
    console.print(f"  content hash: {version.get('content_hash')}")


@adversarial_app.command("generate")
def adversarial_generate(
    dataset: str = typer.Option(..., "--dataset", help="Base dataset name (a published version is read from the API)."),
    out: Path = typer.Option(..., "--out", help="Where to write the generated dataset YAML."),
    profile: Path = typer.Option(..., "--profile", help="The application's attack-surface profile (YAML)."),
    seed: int = typer.Option(7, "--seed", help="Same base content + profile + seed = identical output."),
    per_category: int = typer.Option(5, "--per-category", min=1, help="Variants per attack category (at most)."),
    name: str | None = typer.Option(None, "--name", help="Generated dataset's name. Default: <dataset>-safety."),
    dataset_version: str = typer.Option("latest", "--dataset-version", help="Base version number, or 'latest'."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url"),
) -> None:
    """Generate tagged attack variants of a published dataset, as a new dataset file.

    Seven categories: injection_direct, injection_indirect, poisoned_context,
    malformed_tool_args, tool_failure, pii_probe, unauthorized_tool. Each
    variant records its category and source case. Publish the file with
    `agentforge dataset publish` to version and hash it like any other dataset.
    """
    try:
        raw_profile = yaml.safe_load(profile.read_text(encoding="utf-8"))
        profile_data = adversarial.validate_profile(raw_profile)
    except (OSError, yaml.YAMLError, adversarial.ProfileError) as exc:
        console.print(f"[red]Invalid profile:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    with AgentForgeClient(base_url=api_url) as client:
        try:
            base = client.get_dataset_version(dataset, dataset_version)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not read dataset '{dataset}':[/red] {escape(str(exc))}")
            raise typer.Exit(code=1) from None
    generated = adversarial.generate(
        dataset,
        base["content_hash"],
        base["test_cases"],
        profile_data,
        seed=seed,
        per_category=per_category,
        name=name or f"{dataset}-safety",
        profile_label=profile.as_posix(),
    )
    # LF on every OS, so the committed file is byte-identical wherever it's generated.
    out.write_bytes(generated.to_yaml().encode("utf-8"))
    try:
        _name, _desc, cases, defaults = validate_dataset_file(out)
    except DatasetFileError as exc:  # a generator bug, not a user error
        console.print(f"[red]Generated file failed validation:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    console.print(
        f"Wrote {out}: {len(cases)} variant(s) of '{dataset}' v{base['version']} ({base['content_hash']}), seed {seed}"
    )
    for line in generated.header[4:5]:
        console.print(f"  {line}")
    content_hash = adversarial.base_hash_of(defaults, [c.model_dump() for c in cases])
    console.print(f"  content hash once published: {content_hash}")


@app.command()
def evaluate(
    app_name: str = typer.Option(..., "--app", help="Application name."),
    app_version: str = typer.Option(..., "--app-version", help="Application version label."),
    dataset_name: str = typer.Option(..., "--dataset", help="Dataset name (must already be published)."),
    dataset_version: str = typer.Option(
        "latest", "--dataset-version", help="Dataset version number, or 'latest' (latest published)."
    ),
    adapter: str | None = typer.Option(
        None, "--adapter", help="Python adapter 'module.path:function', importable inside the worker image."
    ),
    adapter_url: str | None = typer.Option(
        None, "--adapter-url", help="HTTP adapter URL, as reachable from the worker container."
    ),
    evaluators: str | None = typer.Option(
        None, "--evaluators", help="Comma-separated name or name@version list. Default: all registered."
    ),
    provider_type: str = typer.Option(
        LOCAL_DETERMINISTIC,
        "--provider-type",
        help=f"What produced the answers. '{LOCAL_DETERMINISTIC}' results are labeled fixture-based.",
    ),
    threshold: float = typer.Option(0.7, "--threshold", help="Pass threshold for 0..1 quality scores."),
    max_latency_ms: float | None = typer.Option(None, "--max-latency-ms", help="Latency budget per case (ms)."),
    timeout: float = typer.Option(10.0, "--timeout", help="Per-case adapter timeout in seconds."),
    environment: str = typer.Option("local", "--environment", help="Free-text environment label."),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Poll until the run finishes."),
    poll_interval: float = typer.Option(1.0, "--poll-interval", help="Seconds between status checks."),
    wait_timeout: float = typer.Option(600.0, "--wait-timeout", help="Stop waiting after this many seconds."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", help="AgentForge API base URL."),
) -> None:
    """Submit an evaluation run to the API and wait for the worker to finish it.

    Nothing executes in this process: the worker (in Docker) runs the adapter
    against every case of the published dataset version and scores it.
    Exit code: 0 completed, 1 failed or setup error, 2 still unfinished when
    --wait-timeout elapsed.
    """
    if (adapter is None) == (adapter_url is None):
        raise typer.BadParameter("pass exactly one of --adapter or --adapter-url")
    try:
        if adapter is not None:
            adapter_spec = AdapterSpec(type="python", target=adapter)
        else:
            assert adapter_url is not None  # exactly one was given (checked above)
            adapter_spec = AdapterSpec(type="http", target=adapter_url)
    except ValidationError as exc:
        raise typer.BadParameter(exc.errors()[0]["msg"]) from exc
    evaluator_list = [e.strip() for e in evaluators.split(",") if e.strip()] if evaluators else None

    with AgentForgeClient(base_url=api_url) as client:
        try:
            application = client.upsert_application(name=app_name)
            app_version_row = client.upsert_application_version(application_id=application["id"], version=app_version)
            dataset_version_row = client.get_dataset_version(dataset_name, dataset_version)
            run = client.create_run(
                {
                    "application_id": application["id"],
                    "application_version_id": app_version_row["id"],
                    "dataset_version_id": dataset_version_row["id"],
                    "adapter": adapter_spec.model_dump(),
                    "evaluators": evaluator_list,
                    "provider_type": provider_type,
                    "threshold": threshold,
                    "max_latency_ms": max_latency_ms,
                    "timeout_seconds": timeout,
                    "environment": environment,
                    "git_commit_sha": current_commit_sha(),
                }
            )
        except Exception as exc:  # noqa: BLE001 - surface any API/network error to the user
            console.print(f"[red]Setup failed:[/red] {exc}")
            raise typer.Exit(code=1) from None

        console.print(
            f"Submitted run [bold]{run['id']}[/bold]: {app_name}@{app_version} against "
            f"{dataset_name} v{dataset_version_row['version']} ({len(dataset_version_row['test_cases'])} case(s)), "
            f"adapter {adapter_spec.type}:{adapter_spec.target}"
        )
        if not wait:
            console.print(f"Not waiting. Check it with: agentforge runs show {run['id']}")
            return

        deadline = time.monotonic() + wait_timeout
        last_line = ""
        while run["status"] not in FINISHED:
            if time.monotonic() > deadline:
                console.print(f"[yellow]Still {run['status']} after {wait_timeout:g}s; stopped waiting.[/yellow]")
                raise typer.Exit(code=2)
            time.sleep(poll_interval)
            try:
                run = client.get_run(run["id"])
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Lost contact with the API while waiting:[/red] {exc}")
                raise typer.Exit(code=1) from None
            line = f"  {run['status']}: {run['progress']['completed_cases']}/{run['progress']['total_cases']} case(s)"
            if line != last_line:
                console.print(line)
                last_line = line

    _print_run(run)
    if run["status"] == "failed":
        raise typer.Exit(code=1)


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    return "-" if value is None else f"{value:{spec}}{suffix}"


def _metric_value(metric: dict[str, Any]) -> str:
    if metric["score"] is not None:
        return f"{metric['score']:.2f}"
    if metric["value"] is not None:
        return f"{metric['value']:.6g}{metric['unit'] or ''}"
    return "n/a"


def _print_run(run: dict[str, Any]) -> None:
    labels = f" [{', '.join(run['labels'])}]" if run["labels"] else ""
    console.print(
        f"\nRun {run['id']} - {run['application_name']}@{run['application_version']} - "
        f"[bold]{run['status']}[/bold]{labels}"
    )
    if run.get("error_message"):
        console.print(f"[red]Run error:[/red] {run['error_message']}")

    table = Table(title="Per-case results")
    table.add_column("Case")
    table.add_column("Status")
    table.add_column("Passed")
    table.add_column("Latency (ms)", justify="right")
    table.add_column("Failed checks")
    for r in run.get("results", []):
        passed = "-" if r["passed"] is None else ("PASS" if r["passed"] else "FAIL")
        failed = [f"{m['evaluator_name']}={_metric_value(m)}" for m in r["metrics"] if m["passed"] is False]
        table.add_row(r["case_key"], r["status"], passed, f"{r['latency_ms']:.1f}", ", ".join(failed) or "-")
    console.print(table)

    for r in run.get("results", []):
        if r["status"] != "ok":
            first_line = (r["error_message"] or "").splitlines()[0] if r["error_message"] else ""
            console.print(f"  [yellow]{r['case_key']}[/yellow] {r['status']}: {r['error_type']}: {first_line}")

    agg = run.get("aggregates")
    if agg:
        lat = agg["latency_ms"]
        console.print(
            f"\n{agg['case_count']} case(s): {agg['passed_count']} passed, {agg['ok_count']} ok, "
            f"{agg['error_count']} error, {agg['timeout_count']} timeout - "
            f"pass_rate=[bold]{_fmt(agg['pass_rate'], '.0%')}[/bold] (threshold={run['threshold']})"
        )
        console.print(
            f"latency p50={_fmt(lat['p50'], '.1f', 'ms')} p95={_fmt(lat['p95'], '.1f', 'ms')} ({lat['basis']})"
        )
        cost = agg.get("estimated_cost_usd")
        if cost:
            console.print(
                f"estimated cost (ESTIMATED from config/pricing.yaml): {_fmt(cost['total'], '.6f', ' USD')} "
                f"over {cost['cases_with_estimate']} case(s); {cost['cases_without_estimate']} without an estimate"
            )
        safety = agg.get("safety")
        if safety:
            by_cat = Table(title=f"Safety: pass rate per attack category ({safety['cases']} adversarial case(s))")
            for col in ("Category", "Cases", "Passed", "Pass rate"):
                by_cat.add_column(col)
            for category, c in safety["by_category"].items():
                by_cat.add_row(category, str(c["cases"]), str(c["passed"]), _fmt(c["pass_rate"], ".0%"))
            console.print(by_cat)
        means = Table(title="Per-metric means")
        means.add_column("Evaluator")
        means.add_column("Mean score")
        means.add_column("Mean value")
        means.add_column("Pass rate")
        means.add_column("n/a")
        for key, m in agg["metrics"].items():
            unit = f" {m['unit']}" if m.get("unit") else ""
            means.add_row(
                key,
                _fmt(m["mean_score"], ".3f"),
                _fmt(m["mean_value"], ".6g", unit),
                _fmt(m["pass_rate"], ".0%"),
                str(m["not_applicable_count"]),
            )
        console.print(means)

    if FIXTURE_BASED_LABEL in run["labels"]:
        console.print(
            "fixture-based: produced by the local-deterministic provider (synthetic app + deterministic "
            "heuristics). Not model quality, and not an LLM judgment."
        )
    console.print(
        f"provider={run['provider_type']}  dataset={run['dataset_name']} v{run['dataset_version']}  "
        f"commit={run.get('git_commit_sha') or '(none)'}"
    )
    if run.get("dataset_content_hash"):
        console.print(f"dataset content hash: {run['dataset_content_hash']}")


@app.command()
def replay(
    result_id: str = typer.Argument(..., help="A case's result id in a run (results[].id, e.g. from GET /runs/{id})."),
    set_: list[str] = typer.Option(
        [],
        "--set",
        help="Override key=value (repeatable). The value is parsed as JSON when it is JSON (3, true, {...}), "
        "otherwise taken as a string. Which keys exist is declared by the adapter.",
    ),
    prompt_file: Path | None = typer.Option(
        None, "--prompt-file", help="Set the `prompt` override to this file's text (adapters that declare one)."
    ),
    retrieval_config: Path | None = typer.Option(
        None, "--retrieval-config", help="Set the `retrieval_config` override from this YAML/JSON object."
    ),
    poll_interval: float = typer.Option(0.5, "--poll-interval", help="Seconds between status checks."),
    wait_timeout: float = typer.Option(300.0, "--wait-timeout", help="Stop waiting after this many seconds."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", help="AgentForge API base URL."),
) -> None:
    """Re-run one evaluated case (same input, scenario, dataset version and
    evaluator versions), optionally with overrides, and print it against the
    original. Executed by the worker, like runs.

    Exit code: 0 completed, 1 failed (including overrides the adapter rejects)
    or setup error, 2 still unfinished when --wait-timeout elapsed.
    """
    try:
        overrides = build_overrides(set_, prompt_file, retrieval_config)
    except OverrideArgsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    with AgentForgeClient(base_url=api_url) as client:
        try:
            current = client.create_replay(result_id, overrides)
        except Exception as exc:  # noqa: BLE001 - surface any API/network error to the user
            console.print(f"[red]Replay not created:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1) from None
        console.print(f"Submitted replay [bold]{current['id']}[/bold] of {escape(current['case_key'])}")
        deadline = time.monotonic() + wait_timeout
        while current["status"] not in FINISHED:
            if time.monotonic() > deadline:
                console.print(f"[yellow]Still {current['status']} after {wait_timeout:g}s; stopped waiting.[/yellow]")
                raise typer.Exit(code=2)
            time.sleep(poll_interval)
            try:
                current = client.get_replay(current["id"])
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]Lost contact with the API while waiting:[/red] {escape(str(exc))}")
                raise typer.Exit(code=1) from None
    print_replay(console, current)
    if current["status"] == "failed":
        raise typer.Exit(code=1)


@runs_app.command("list")
def runs_list(api_url: str = typer.Option(DEFAULT_API_URL, "--api-url")) -> None:
    """List evaluation runs, most recent first."""
    with AgentForgeClient(base_url=api_url) as client:
        try:
            runs = client.list_runs()
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not reach API:[/red] {exc}")
            raise typer.Exit(code=1) from None

    if not runs:
        console.print("No runs yet. Run `agentforge evaluate ...` first.")
        return

    table = Table(title="Evaluation runs")
    table.add_column("Run ID")
    table.add_column("Application")
    table.add_column("Dataset")
    table.add_column("Status")
    table.add_column("Pass rate")
    table.add_column("Cases")
    table.add_column("Labels")
    table.add_column("Created")
    for run in runs:
        agg = run["aggregates"] or {}
        table.add_row(
            run["id"],
            f"{run['application_name']}@{run['application_version']}",
            f"{run['dataset_name']} v{run['dataset_version']}",
            run["status"],
            _fmt(agg.get("pass_rate"), ".0%"),
            f"{run['progress']['completed_cases']}/{run['progress']['total_cases']}",
            ", ".join(run["labels"]),
            run["created_at"],
        )
    console.print(table)


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="Evaluation run id."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url"),
) -> None:
    """Show one evaluation run's full detail."""
    with AgentForgeClient(base_url=api_url) as client:
        try:
            run = client.get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not fetch run:[/red] {exc}")
            raise typer.Exit(code=1) from None
    _print_run(run)


# -- baselines, regression, release gate -------------------------------------------


@baseline_app.command("set")
def baseline_set(
    run_id: str = typer.Argument(..., help="A completed run id."),
    env: str = typer.Option(..., "--env", help="Environment name, e.g. production."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url"),
) -> None:
    """Point (the run's application, ENV, the run's dataset) at RUN_ID. Only the pointer changes."""
    with AgentForgeClient(base_url=api_url) as client:
        try:
            row = client.set_baseline(run_id, env)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not set baseline:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1) from None
    console.print(
        f"Baseline [bold]{row['application_name']}[/bold] / [bold]{row['environment']}[/bold] -> run {row['run_id']} "
        f"({row['application_version']}, {row['dataset_name']} v{row['dataset_version']}, "
        f"pass_rate={fmt_number(row['pass_rate'])})"
    )


@baseline_app.command("show")
def baseline_show(
    app_name: str | None = typer.Option(None, "--app", help="Only this application."),
    env: str | None = typer.Option(None, "--env", help="Only this environment."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url"),
) -> None:
    """List baseline pointers."""
    with AgentForgeClient(base_url=api_url) as client:
        try:
            rows = client.list_baselines(app_name, env)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not list baselines:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1) from None
    if not rows:
        console.print("No baselines set. Set one with: agentforge baseline set <run_id> --env <environment>")
        return
    table = Table(title="Baselines")
    for col in ("Application", "Environment", "Run", "App version", "Dataset", "Pass rate", "Set at"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["application_name"],
            r["environment"],
            r["run_id"],
            r["application_version"],
            f"{r['dataset_name']} v{r['dataset_version']}",
            fmt_number(r["pass_rate"]),
            r["set_at"],
        )
    console.print(table)


def _print_regression(report: dict[str, Any]) -> None:
    table = Table(title="Run-level deltas (candidate - baseline)")
    for col in ("Metric", "Baseline", "Candidate", "Delta"):
        table.add_column(col)
    for metric, d in report["summary"].items():
        table.add_row(metric, fmt_number(d["baseline"], metric), fmt_number(d["candidate"], metric), fmt_delta(d))
    console.print(table)
    table = Table(title="Per-evaluator deltas")
    for col in ("Evaluator", "Mean score", "Pass rate", "Mean value", "Note"):
        table.add_column(col)
    for m in report["metrics"]:
        note = "" if m["comparable"] else f"not comparable ({m['baseline_version']} vs {m['candidate_version']})"
        table.add_row(
            m["name"],
            fmt_delta(m["mean_score"]),
            fmt_delta(m["pass_rate"]),
            fmt_delta(m["mean_value"]),
            note,
        )
    console.print(table)
    counts = report["case_counts"]
    console.print("Cases: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()))
    for case in report["cases"]["newly_failing"]:
        # escape(): Rich would read "[refund, critical]" as markup and drop it.
        tags = escape(f" [{', '.join(case['tags'])}]") if case["tags"] else ""
        failed_by = ", ".join(case["candidate_failed_evaluators"]) or case["candidate_status"]
        console.print(f"  newly failing: {case['case_key']}{tags}: {failed_by}")
    for case in report["cases"]["fixed"]:
        console.print(f"  fixed: {case['case_key']}")


@app.command()
def compare(
    baseline: str | None = typer.Option(None, "--baseline", help="Baseline run id (run-vs-run regression)."),
    candidate: str | None = typer.Option(None, "--candidate", help="Candidate run id (run-vs-run regression)."),
    models: str | None = typer.Option(
        None,
        "--models",
        help="Model comparison: comma-separated specs, e.g. scripted,anthropic:claude-haiku-4-5,ollama:llama3.1:8b.",
    ),
    plan_file: Path | None = typer.Option(
        None,
        "--plan",
        help="Model comparison from a plan file: models in run order, each with its datasets, repeats and timeout "
        "(the default comparison: benchmarks/compare.yaml). Replaces --models/--datasets/--repeats.",
    ),
    datasets: str = typer.Option(
        "invoice-agent,invoice-agent-safety", "--datasets", help="Comma-separated published dataset names."
    ),
    repeats: int = typer.Option(1, "--repeats", min=1, max=20, help="Runs per model and dataset."),
    max_cost: float = typer.Option(5.0, "--max-cost", min=0, help="Spend cap in USD (estimated) for the whole run."),
    yes: bool = typer.Option(
        False, "--yes", help="Start even if the estimate is over --max-cost (the cap still holds)."
    ),
    prompt_file: Path | None = typer.Option(None, "--prompt-file", help="Base system prompt for the LLM planner."),
    temperature: float = typer.Option(0.0, "--temperature", min=0, max=2, help="Sampling temperature (LLM models)."),
    timeout: float = typer.Option(
        120.0, "--timeout", min=1, max=600, help="Per-case timeout (s) for LLM models; a plan entry may set its own."
    ),
    pricing_file: Path = typer.Option(Path("config/pricing.yaml"), "--pricing-file", help="Pricing table."),
    env_file: Path = typer.Option(Path(".env"), "--env-file", help="Where keys are looked up besides the env."),
    output: Path | None = typer.Option(None, "--output", help="Results JSON (default: benchmarks/<date>-....json)."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url"),
) -> None:
    """Regression report between two runs (--baseline/--candidate), or a model comparison (--models / --plan)."""
    if models is not None or plan_file is not None:
        if baseline or candidate:
            raise typer.BadParameter("use either --models/--plan or --baseline/--candidate, not both")
        if models is not None and plan_file is not None:
            raise typer.BadParameter("use either --models or --plan, not both")
        if plan_file is not None:
            try:
                plan_spec = benchmark.load_plan_file(plan_file)
            except benchmark.BenchmarkError as exc:
                raise typer.BadParameter(str(exc)) from exc
        else:
            try:
                specs = list(dict.fromkeys(benchmark.parse_model_spec(m) for m in benchmark.split_list(models or "")))
            except benchmark.BenchmarkError as exc:
                raise typer.BadParameter(str(exc)) from exc
            names = tuple(benchmark.split_list(datasets))
            if not specs or not names:
                raise typer.BadParameter("--models and --datasets need at least one entry each")
            plan_spec = benchmark.PlanFile(specs, {s.raw: benchmark.ModelPlan(names, repeats) for s in specs})
        _compare_models(
            plan_spec, repeats, max_cost, yes, prompt_file, temperature, timeout, pricing_file, env_file, output,
            api_url,
        )  # fmt: skip
        return
    if not (baseline and candidate):
        raise typer.BadParameter("pass --baseline and --candidate (two run ids), --models, or --plan")
    with AgentForgeClient(base_url=api_url) as client:
        try:
            report = client.regression(baseline, candidate)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not compare:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1) from None
    _print_regression(report)


def _planner_texts(prompt_file: Path | None) -> tuple[str | None, str, int, int]:
    """(prompt to send or None for the built-in, prompt source label, prompt chars, tool-schema chars)."""
    if prompt_file is not None:
        text = prompt_file.read_text(encoding="utf-8")
        source = f"file {prompt_file.as_posix()}"
    else:
        text, source = None, "built-in (examples/invoice_agent/invoice_agent/prompts/system.md)"
    try:  # sizes for the estimate, from the agent itself when it's installed here
        from invoice_agent.llm_planner import builtin_prompt, tool_schemas
        from invoice_agent.tools import TOOLS

        prompt_chars = len(text if text is not None else builtin_prompt())
        schema_chars = len(json.dumps(tool_schemas(TOOLS)))
    except ImportError:
        prompt_chars, schema_chars = len(text or "") or 3000, 4000
    return text, source, prompt_chars, schema_chars


def _prompt_hash(prompt: str | None) -> str | None:
    if prompt is not None:
        return benchmark.sha256_text(prompt)
    try:
        from invoice_agent.llm_planner import builtin_prompt

        return benchmark.sha256_text(builtin_prompt())
    except ImportError:
        return None


def _cell(summary: dict[str, Any], kind: str) -> str:
    mean = summary["mean"]
    if mean is None:
        return "-"
    fmt = {"rate": "{:.2f}", "ms": "{:.0f}", "tokens": "{:.0f}", "usd": "{:.5f}"}[kind]
    text = fmt.format(mean)
    if summary["n"] > 1 and summary["min"] != summary["max"]:
        text += f" ({fmt.format(summary['min'])}-{fmt.format(summary['max'])})"
    return text


_COLUMNS = (
    ("pass_rate", "pass", "rate"),
    ("tool_selection", "tool_sel", "rate"),
    ("approval_required", "approval", "rate"),
    ("injection_direct", "inj direct", "rate"),
    ("injection_indirect", "inj indirect", "rate"),
    ("unauthorized_tool", "unauth tool", "rate"),
    ("pii_probe", "pii probe", "rate"),
    ("latency_p50_ms", "P50 ms", "ms"),
    ("latency_p95_ms", "P95 ms", "ms"),
    ("tokens_per_case", "tokens/case", "tokens"),
    ("cost_per_case_usd", "est. $/case", "usd"),
)


def _compare_models(
    plan_spec: benchmark.PlanFile,
    repeats: int,
    max_cost: float,
    yes: bool,
    prompt_file: Path | None,
    temperature: float,
    timeout: float,
    pricing_file: Path,
    env_file: Path,
    output: Path | None,
    api_url: str,
) -> None:
    specs = plan_spec.specs
    dataset_names = plan_spec.dataset_names
    try:
        pricing = parse_pricing(yaml.safe_load(pricing_file.read_text(encoding="utf-8")))
    except (OSError, PricingConfigError, yaml.YAMLError) as exc:
        console.print(f"[red]Can't read the pricing file {escape(str(pricing_file))}:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from None
    prompt, prompt_source, prompt_chars, schema_chars = _planner_texts(prompt_file)
    env = benchmark.environment(env_file)
    ollama_host = env.get("OLLAMA_HOST", "").strip() or benchmark.DEFAULT_OLLAMA_HOST
    ollama_cache: list[list[str] | None] = []

    def ollama() -> list[str] | None:
        if not ollama_cache:
            ollama_cache.append(benchmark.ollama_models(ollama_host))
        return ollama_cache[0]

    skipped = {s.raw: why for s in specs if (why := benchmark.skip_reason(s, env, pricing, ollama)) is not None}
    for raw, why in skipped.items():
        console.print(f"[yellow]Skipping {escape(raw)}:[/yellow] {escape(why)}")

    with AgentForgeClient(base_url=api_url) as client:
        try:
            versions = [client.get_dataset_version(name, "latest") for name in dataset_names]
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not load the datasets:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1) from None
        dataset_rows = [
            {
                "id": v["id"],
                "name": name,
                "version": v["version"],
                "content_hash": v.get("content_hash"),
                "cases": len(v["test_cases"]),
            }
            for name, v in zip(dataset_names, versions, strict=True)
        ]
        base_tokens = benchmark.base_input_tokens(prompt_chars, schema_chars)
        runnable = [s for s in specs if s.raw not in skipped]
        plan = benchmark.Plan(specs, skipped, dataset_rows, repeats, [], plan_spec.entries)
        plan.estimates = [
            benchmark.estimate(
                s, sum(d["cases"] for d in plan.datasets_for(s)), plan.repeats_for(s), base_tokens, pricing
            )
            for s in runnable
        ]
        if not runnable:
            console.print("[red]No model can run here; nothing was started.[/red]")
            raise typer.Exit(code=1)

        table = Table(title="Estimated cost before running (rough, deliberately high)")
        for col in ("model", "cases x repeats", "tokens/case in+out", "est. USD"):
            table.add_column(col)
        for e in plan.estimates:
            table.add_row(
                escape(e.spec),
                f"{e.cases} x {e.runs}",
                f"{e.input_tokens_per_case}+{e.output_tokens_per_case}" if e.input_tokens_per_case else "- (no model)",
                f"${e.usd:.4f}",
            )
        table.add_row("[bold]total[/bold]", "", "", f"[bold]${plan.total_estimate:.4f}[/bold]")
        console.print(table)
        console.print(f"Assumptions: {escape(benchmark.estimate_assumptions())}")
        if plan.total_estimate > max_cost and not yes:
            console.print(
                f"[red]Refusing to start: the estimate ${plan.total_estimate:.4f} is over "
                f"--max-cost ${max_cost:g}.[/red] Lower --repeats or the model list, raise --max-cost, "
                "or pass --yes (the cap is still enforced)."
            )
            raise typer.Exit(code=1)

        started_at = benchmark.now()
        try:
            outcome = benchmark.execute(
                client,
                plan,
                temperature=temperature,
                prompt=prompt,
                max_cost=max_cost,
                timeout_seconds=timeout,
                pricing=pricing,
                git_commit_sha=current_commit_sha(),
                log=lambda line: console.print(escape(line)),
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Comparison stopped:[/red] {escape(str(exc))}")
            raise typer.Exit(code=1) from None

    doc = benchmark.document(
        plan,
        outcome,
        created_at=started_at,
        temperature=temperature,
        prompt_source=prompt_source,
        prompt_sha256=_prompt_hash(prompt),
        max_cost=max_cost,
        git_commit_sha=current_commit_sha(),
        timeout_seconds=timeout,
    )
    benchmark.validate_document(doc)

    results = Table(title="Model comparison (mean over completed repeats; min-max in parentheses)")
    results.add_column("model")
    results.add_column("dataset")
    for _key, header, _kind in _COLUMNS:
        results.add_column(header, justify="right")
    for row in doc["results"]:
        results.add_row(
            escape(row["model"]),
            escape(row["dataset"]),
            *(_cell(row["summary"][key], kind) for key, _header, kind in _COLUMNS),
        )
    console.print(results)
    console.print(
        "Synthetic invoice tasks scored by deterministic evaluators (no LLM judge); cost is estimated "
        f"(reported tokens x {escape(pricing_file.as_posix())}). Actual estimated spend: ${outcome.spent_usd:.4f}."
    )
    if outcome.stopped_reason:
        console.print(f"[yellow]{escape(outcome.stopped_reason)}[/yellow]")
    path = output or benchmark.default_output_path(Path("benchmarks"), started_at, doc)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    console.print(f"Saved {escape(path.as_posix())}")


@app.command()
def gate(
    candidate: str = typer.Option(..., "--candidate", help="Candidate run id."),
    baseline: str = typer.Option(..., "--baseline", help="Baseline environment (e.g. production) or run id."),
    config: Path = typer.Option(Path("agentforge.yaml"), "--config", help="Project config with release_policy."),
    policy_key: str = typer.Option(
        "release_policy", "--policy", help="Which policy in the config to apply: release_policy or safety_policy."
    ),
    api_url: str | None = typer.Option(None, "--api-url", help="Default: api_url from the config, else localhost."),
    markdown: Path | None = typer.Option(
        None, "--markdown", help="Also write the Markdown report to this file (e.g. for a PR comment)."
    ),
    title: str | None = typer.Option(
        None, "--title", help="Write the Markdown report as a titled section (for combining several gates)."
    ),
) -> None:
    """Evaluate the release policy for CANDIDATE against BASELINE.

    The API computes every check from the persisted runs (pure arithmetic,
    no LLM) and stores an immutable ReleaseDecision. Writes a Markdown report
    to $GITHUB_STEP_SUMMARY when it's set. Exit code: 0 PASSED, 1 FAILED,
    2 the gate couldn't be evaluated (bad config, missing run or baseline, API error).
    """
    try:
        project = load_config(config)
    except ConfigError as exc:
        console.print(f"[red]Invalid config:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from None
    policy = project.policies.get(policy_key)
    if policy is None:
        console.print(f"[red]Invalid config:[/red] {config} has no {escape(policy_key)}")
        raise typer.Exit(code=2)
    with AgentForgeClient(base_url=api_url or project.api_url or DEFAULT_API_URL) as client:
        try:
            decision = client.create_release_decision(candidate, baseline, policy.to_dict())
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Gate could not be evaluated:[/red] {escape(str(exc))}")
            raise typer.Exit(code=2) from None

    console.print(
        f"Release gate for [bold]{decision['application_name']}[/bold]: candidate {decision['candidate_run_id']} "
        f"vs baseline '{decision['baseline_ref']}' (run {decision['baseline_run_id']})"
    )
    table = Table(title=f"Checks ({config}: {policy_key})")
    for col in ("Result", "Check", "Metric", "Baseline", "Candidate", "Delta", "Threshold", "Reason"):
        table.add_column(col, overflow="fold")
    for c in decision["checks"]:
        table.add_row(
            "pass" if c["passed"] else "[red]FAIL[/red]",
            c["kind"],
            escape(c["metric"]),  # "newly_failing[tag=critical]" would be read as markup
            fmt_number(c["baseline"], c["metric"]),
            fmt_number(c["candidate"], c["metric"]),
            fmt_delta(c),
            escape(c["rule"]),
            escape(c["reason"]),
        )
    console.print(table)
    counts = decision["regression"]["case_counts"]
    console.print("Cases: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in counts.items()))
    for case in decision["regression"]["cases"]["newly_failing"]:
        # escape(): Rich would read "[refund, critical]" as markup and drop it.
        tags = escape(f" [{', '.join(case['tags'])}]") if case["tags"] else ""
        failed_by = ", ".join(case["candidate_failed_evaluators"]) or case["candidate_status"]
        console.print(f"  newly failing: {case['case_key']}{tags}: {failed_by}")
    console.print(f"Decision {decision['id']} recorded (immutable).")

    if markdown is not None:
        markdown.write_text(markdown_report(decision, title), encoding="utf-8")
        console.print(f"Markdown report written to {markdown}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(markdown_report(decision, title))
        console.print(f"Markdown report appended to {summary_path}")

    # Plain print: the last line is exactly this, for scripts and CI logs.
    print(verdict_line(decision["passed"]))
    if not decision["passed"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
