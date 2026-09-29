"""AgentForge CLI (Phase 1): dataset publish/validate, evaluate, runs list/show.

No queue, no worker, no release gate in this phase -- `evaluate` executes an
adapter in-process, scores it with the one deterministic evaluator, and
persists everything to the API synchronously.
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from typing import Any, Callable

import typer
from rich.console import Console
from rich.table import Table

# Windows consoles/pipes don't reliably default to UTF-8 (often cp1252),
# but Rich's table borders and our own output assume UTF-8. Force it so
# output is correct both in a real terminal and when captured by a test or
# CI runner, regardless of the host's console code page.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

from agentforge_core.schemas import ResultStatus
from agentforge_evaluators import EVALUATOR_NAME, EVALUATOR_VERSION, heuristic_context_precision
from agentforge_sdk import AgentForgeClient

from agentforge_cli.dataset_io import DatasetFileError, validate_dataset_file
from agentforge_cli.git_utils import current_commit_sha

app = typer.Typer(add_completion=False, help="AgentForge command-line interface.")
dataset_app = typer.Typer(add_completion=False, help="Manage evaluation datasets.")
runs_app = typer.Typer(add_completion=False, help="Inspect evaluation runs.")
app.add_typer(dataset_app, name="dataset")
app.add_typer(runs_app, name="runs")

console = Console()

DEFAULT_API_URL = "http://127.0.0.1:8000"

AGENTFORGE_YAML_TEMPLATE = """\
# AgentForge project configuration.
#
# Phase 1 does not read this file yet -- application/dataset/evaluator names
# are passed as CLI flags (see README). It is scaffolded now so release-gate
# configuration (added in a later phase) has a home without another
# breaking change to project layout.

api_url: {api_url}

# release:
#   minimum: {{}}
#   maximum: {{}}
#   regression: {{}}
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
        name, _description, test_cases = validate_dataset_file(file)
    except DatasetFileError as exc:
        console.print(f"[red]Invalid dataset:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(f"[green]Valid[/green] dataset '{name}' with {len(test_cases)} test case(s):")
    for tc in test_cases:
        console.print(f"  - {tc.case_key} ({len(tc.expected_context)} expected doc id(s))")


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
        name, description, test_cases = validate_dataset_file(file)
    except DatasetFileError as exc:
        console.print(f"[red]Invalid dataset:[/red] {exc}")
        raise typer.Exit(code=1)

    with AgentForgeClient(base_url=api_url) as client:
        try:
            dataset = client.upsert_dataset(name=name, description=description)
            draft = client.create_draft_version(
                dataset_id=dataset["id"],
                test_cases=[tc.model_dump() for tc in test_cases],
            )
            version = client.publish_dataset_version(dataset_name=name, version=draft["version"])
        except Exception as exc:  # noqa: BLE001 - surface any API/network error to the user
            console.print(f"[red]Failed to publish dataset:[/red] {exc}")
            raise typer.Exit(code=1)

    console.print(
        f"[green]Published[/green] dataset '{name}' version {version['version']} "
        f"({len(version['test_cases'])} test case(s), id={version['id']})"
    )


def _load_adapter(adapter_path: str) -> Callable[[str], Any]:
    if ":" not in adapter_path:
        raise typer.BadParameter("--adapter must be in 'module.path:function_name' form")
    module_path, func_name = adapter_path.split(":", 1)
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise typer.BadParameter(f"could not import adapter module '{module_path}': {exc}")
    func = getattr(module, func_name, None)
    if func is None or not callable(func):
        raise typer.BadParameter(f"module '{module_path}' has no callable '{func_name}'")
    return func


@app.command()
def evaluate(
    app_name: str = typer.Option(..., "--app", help="Application name."),
    app_version: str = typer.Option(..., "--app-version", help="Application version label."),
    dataset_name: str = typer.Option(..., "--dataset", help="Dataset name (must already be published)."),
    dataset_version: str = typer.Option(
        "latest", "--dataset-version", help="Dataset version number, or 'latest'."
    ),
    adapter: str = typer.Option(..., "--adapter", help="Adapter entry point: 'module.path:function_name'."),
    threshold: float = typer.Option(0.7, "--threshold", help="Score >= threshold counts as passed."),
    environment: str = typer.Option("local", "--environment", help="Free-text environment label."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url", help="AgentForge API base URL."),
) -> None:
    """Execute an application adapter against a published dataset version and persist results.

    This is a trusted, in-process, synchronous execution: the adapter runs
    directly in this CLI process (no sandbox, no queue/worker). Only the
    local-deterministic provider is used -- no external model API call is
    made.
    """
    adapter_fn = _load_adapter(adapter)
    commit_sha = current_commit_sha()

    with AgentForgeClient(base_url=api_url) as client:
        try:
            application = client.upsert_application(name=app_name)
            app_version_row = client.upsert_application_version(
                application_id=application["id"], version=app_version
            )
            dataset_version_row = client.get_dataset_version(dataset_name, dataset_version)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Setup failed:[/red] {exc}")
            raise typer.Exit(code=1)

        test_cases = dataset_version_row["test_cases"]
        console.print(
            f"Evaluating [bold]{app_name}[/bold]@{app_version} against "
            f"[bold]{dataset_name}[/bold] v{dataset_version_row['version']} "
            f"({len(test_cases)} case(s)) using provider [bold]local-deterministic[/bold]..."
        )

        try:
            run = client.create_run(
                {
                    "application_id": application["id"],
                    "application_version_id": app_version_row["id"],
                    "dataset_version_id": dataset_version_row["id"],
                    "provider_type": "local-deterministic",
                    "evaluator_name": EVALUATOR_NAME,
                    "evaluator_version": EVALUATOR_VERSION,
                    "environment": environment,
                    "threshold": threshold,
                    "git_commit_sha": commit_sha,
                }
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not create run:[/red] {exc}")
            raise typer.Exit(code=1)

        results_payload: list[dict[str, Any]] = []
        for tc in test_cases:
            start = time.perf_counter()
            try:
                output = adapter_fn(tc["input"])
                latency_ms = int((time.perf_counter() - start) * 1000)
                scored = heuristic_context_precision(
                    retrieved_doc_ids=output.retrieved_doc_ids,
                    expected_relevant_doc_ids=tc["expected_context"],
                )
                results_payload.append(
                    {
                        "test_case_id": tc["id"],
                        "retrieved_doc_ids": output.retrieved_doc_ids,
                        "output_answer": output.answer,
                        "score": scored.score,
                        "passed": scored.score >= threshold,
                        "evidence": scored.evidence,
                        "latency_ms": latency_ms,
                        "status": ResultStatus.ok.value,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one bad case must not kill the whole run
                latency_ms = int((time.perf_counter() - start) * 1000)
                results_payload.append(
                    {
                        "test_case_id": tc["id"],
                        "retrieved_doc_ids": [],
                        "output_answer": None,
                        "score": None,
                        "passed": None,
                        "evidence": {},
                        "latency_ms": latency_ms,
                        "status": ResultStatus.error.value,
                        "error_message": str(exc),
                    }
                )

        try:
            client.submit_results(run["id"], results_payload)
            final_run = client.complete_run(run["id"], status="completed")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not persist results:[/red] {exc}")
            try:
                client.complete_run(run["id"], status="failed")
            except Exception:  # noqa: BLE001
                pass
            raise typer.Exit(code=1)

    _print_run(final_run)
    console.print(f"\nRun id: [bold]{final_run['id']}[/bold]")


def _print_run(run: dict[str, Any]) -> None:
    table = Table(title=f"Run {run['id']} - {run['application_name']}@{run['application_version']}")
    table.add_column("Case")
    table.add_column("Score")
    table.add_column("Passed")
    table.add_column("Latency (ms)")
    table.add_column("Status")

    for r in run["results"]:
        score_str = f"{r['score']:.2f}" if r["score"] is not None else "-"
        passed_str = "-" if r["passed"] is None else ("PASS" if r["passed"] else "FAIL")
        table.add_row(r["case_key"], score_str, passed_str, str(r["latency_ms"]), r["status"])

    console.print(table)

    mean_score = f"{run['mean_score']:.3f}" if run["mean_score"] is not None else "-"
    pass_rate = f"{run['pass_rate']:.0%}" if run["pass_rate"] is not None else "-"
    avg_latency = f"{run['avg_latency_ms']:.1f}" if run["avg_latency_ms"] is not None else "-"

    console.print(
        f"\n[bold]{run['case_count']}[/bold] case(s) - "
        f"mean heuristic_context_precision=[bold]{mean_score}[/bold], "
        f"pass_rate=[bold]{pass_rate}[/bold] (threshold={run['threshold']}), "
        f"avg_latency={avg_latency}ms"
    )
    console.print(
        f"evaluator={run['evaluator_name']}@{run['evaluator_version']}  "
        f"provider={run['provider_type']}  dataset={run['dataset_name']} v{run['dataset_version']}  "
        f"commit={run['git_commit_sha'] or '(none)'}"
    )


@runs_app.command("list")
def runs_list(api_url: str = typer.Option(DEFAULT_API_URL, "--api-url")) -> None:
    """List persisted evaluation runs, most recent first."""
    with AgentForgeClient(base_url=api_url) as client:
        try:
            runs = client.list_runs()
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not reach API:[/red] {exc}")
            raise typer.Exit(code=1)

    if not runs:
        console.print("No runs yet. Run `agentforge evaluate ...` first.")
        return

    table = Table(title="Evaluation runs")
    table.add_column("Run ID")
    table.add_column("Application")
    table.add_column("Dataset")
    table.add_column("Status")
    table.add_column("Mean score")
    table.add_column("Pass rate")
    table.add_column("Created")

    for run in runs:
        mean_score = f"{run['mean_score']:.3f}" if run["mean_score"] is not None else "-"
        pass_rate = f"{run['pass_rate']:.0%}" if run["pass_rate"] is not None else "-"
        table.add_row(
            run["id"],
            f"{run['application_name']}@{run['application_version']}",
            f"{run['dataset_name']} v{run['dataset_version']}",
            run["status"],
            mean_score,
            pass_rate,
            run["created_at"],
        )
    console.print(table)


@runs_app.command("show")
def runs_show(
    run_id: str = typer.Argument(..., help="Evaluation run id."),
    api_url: str = typer.Option(DEFAULT_API_URL, "--api-url"),
) -> None:
    """Show one evaluation run's full detail, including per-case evidence."""
    with AgentForgeClient(base_url=api_url) as client:
        try:
            run = client.get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not fetch run:[/red] {exc}")
            raise typer.Exit(code=1)
    _print_run(run)


if __name__ == "__main__":
    app()
