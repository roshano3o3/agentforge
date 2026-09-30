"""End-to-end: the real `agentforge` CLI (subprocess) -> a real uvicorn API ->
real Redis -> the real worker running in its Docker container -> Postgres.
Nothing is mocked and nothing executes in the test process.

Needs the Postgres test mode plus a test Redis and worker:
`.\\scripts\\test.ps1 -Postgres` starts a worker container bound to
AGENTFORGE_TEST_REDIS_URL's queue and the agentforge_test database (CI does
the same with `docker run`). Without them, the tests that need the worker are
skipped; the one that fails before anything is queued still runs on SQLite.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "apps" / "api"
TEST_REDIS_URL = os.environ.get("AGENTFORGE_TEST_REDIS_URL") or None
# Nothing listens on port 1: in SQLite mode any enqueue fails fast rather than
# reaching the dev queue.
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _create_sqlite_schema(env: dict[str, str]) -> None:
    create_schema = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio\n"
            # Importing agentforge_api.models registers all tables on
            # Base.metadata -- without this, create_all() silently creates
            # an empty database (no error, zero tables).
            "import agentforge_api.models\n"
            "from agentforge_api.db.base import Base, engine\n"
            "async def main():\n"
            "    async with engine.begin() as conn:\n"
            "        await conn.run_sync(Base.metadata.create_all)\n"
            "asyncio.run(main())\n",
        ],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert create_schema.returncode == 0, create_schema.stdout + create_schema.stderr


@pytest.fixture()
def live_api(tmp_path: Path, test_db_url: str | None) -> Iterator[str]:
    port = _free_port()
    env = dict(os.environ)
    if test_db_url is not None:
        # Schema already built by the Alembic migrations; tables emptied by test_db_url.
        env["AGENTFORGE_DATABASE_URL"] = test_db_url
        env["AGENTFORGE_REDIS_URL"] = TEST_REDIS_URL or UNREACHABLE_REDIS
    else:
        env["AGENTFORGE_DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'e2e.db'}"
        env["AGENTFORGE_REDIS_URL"] = UNREACHABLE_REDIS
        _create_sqlite_schema(env)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agentforge_api.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=API_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("test API server did not become healthy in time")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture()
def worker_api(live_api: str, test_db_url: str | None) -> str:
    if test_db_url is None or TEST_REDIS_URL is None:
        pytest.skip("needs Postgres + Redis + the Docker test worker (.\\scripts\\test.ps1 -Postgres)")
    return live_api


def _run_cli(args: list[str], timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentforge_cli.main", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def _evaluate(api: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return _run_cli(
        ["evaluate", "--app", "rag-assistant", "--app-version", "v1", "--api-url", api,
         "--poll-interval", "0.5", "--wait-timeout", "120", *extra]
    )


def test_cli_evaluation_is_executed_by_the_docker_worker(worker_api: str) -> None:
    publish = _run_cli(["dataset", "publish", "datasets/rag_support_v1.yaml", "--api-url", worker_api])
    assert publish.returncode == 0, publish.stdout + publish.stderr

    evaluate = _evaluate(worker_api, "--dataset", "rag-support", "--adapter", "rag_app.adapter:answer")
    assert evaluate.returncode == 0, evaluate.stdout + evaluate.stderr
    out = evaluate.stdout
    assert "Submitted run" in out
    assert "completed" in out
    assert "fixture-based" in out

    # Verify independently through the API what the CLI printed.
    [summary] = httpx.get(f"{worker_api}/runs", timeout=5).json()
    assert summary["status"] == "completed"
    assert summary["labels"] == ["fixture-based"]
    run = httpx.get(f"{worker_api}/runs/{summary['id']}", timeout=5).json()
    assert run["progress"] == {"completed_cases": 5, "total_cases": 5}
    assert all(r["status"] == "ok" for r in run["results"])
    precision = {
        r["case_key"]: next(m for m in r["metrics"] if m["evaluator_name"] == "heuristic_context_precision")
        for r in run["results"]
    }
    # Same deterministic scores as Phase 1's client-side run of this dataset:
    # the evaluator formula didn't change, only where it executes.
    assert precision["warranty-outerwear-001"]["score"] == pytest.approx(1.0)
    assert precision["out-of-scope-sponsorship-001"]["score"] == pytest.approx(0.0)
    assert run["aggregates"]["metrics"]["heuristic_context_precision@1.0.0"]["mean_score"] == pytest.approx(0.5)


def test_faulty_cases_are_contained_by_the_docker_worker(worker_api: str) -> None:
    publish = _run_cli(["dataset", "publish", "datasets/fault_injection_demo.yaml", "--api-url", worker_api])
    assert publish.returncode == 0, publish.stdout + publish.stderr

    evaluate = _evaluate(
        worker_api, "--dataset", "fault-injection-demo", "--adapter", "rag_app.fault_injection:answer",
        "--timeout", "1",
    )
    assert evaluate.returncode == 0, evaluate.stdout + evaluate.stderr

    [summary] = httpx.get(f"{worker_api}/runs", timeout=5).json()
    run = httpx.get(f"{worker_api}/runs/{summary['id']}", timeout=5).json()
    assert run["status"] == "completed"
    statuses = {r["case_key"]: (r["status"], r["error_type"]) for r in run["results"]}
    assert statuses == {
        "good-case": ("ok", None),
        "hangs-past-timeout": ("timeout", "timeout"),
        "raises-exception": ("error", "RuntimeError"),
        "calls-sys-exit": ("error", "SystemExit"),
        "malformed-output": ("error", "agentforge_worker.adapters.AdapterOutputError"),
    }

    # The worker survived all of that: a follow-up run is still executed.
    again = _evaluate(worker_api, "--dataset", "fault-injection-demo", "--adapter", "rag_app.adapter:answer")
    assert again.returncode == 0, again.stdout + again.stderr


def test_cli_evaluate_fails_fast_on_unknown_dataset(live_api: str) -> None:
    evaluate = _evaluate(live_api, "--dataset", "does-not-exist", "--adapter", "rag_app.adapter:answer")
    assert evaluate.returncode == 1
    assert "Setup failed" in evaluate.stdout
    assert "does-not-exist" in evaluate.stdout
