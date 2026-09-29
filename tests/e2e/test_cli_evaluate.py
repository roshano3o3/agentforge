"""End-to-end test: the real `agentforge` CLI, as a subprocess, against a
real (locally spawned) uvicorn server -- no mocking of the CLI, the API, or
the HTTP layer between them.

Uses a temp-file SQLite database rather than Postgres because Docker isn't
available in this environment; the API/ORM code path exercised is identical
to the one that runs against Postgres in Docker Compose.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from collections.abc import Iterator

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "apps" / "api"
DATASET_FILE = REPO_ROOT / "datasets" / "rag_support_v1.yaml"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_api(tmp_path: Path) -> Iterator[str]:
    db_path = tmp_path / "e2e.db"
    port = _free_port()
    env = dict(os.environ)
    env["AGENTFORGE_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"

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
        healthy = False
        for _ in range(50):
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    healthy = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        if not healthy:
            proc.terminate()
            raise RuntimeError("test API server did not become healthy in time")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentforge_cli.main", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def test_cli_five_case_evaluation_end_to_end(live_api: str) -> None:
    publish = _run_cli(["dataset", "publish", str(DATASET_FILE), "--api-url", live_api])
    assert publish.returncode == 0, publish.stdout + publish.stderr
    assert "Published" in publish.stdout
    assert "5 test case(s)" in publish.stdout

    evaluate = _run_cli(
        [
            "evaluate",
            "--app", "rag-assistant",
            "--app-version", "v1",
            "--dataset", "rag-support",
            "--dataset-version", "latest",
            "--adapter", "rag_app.adapter:answer",
            "--api-url", live_api,
        ]
    )
    assert evaluate.returncode == 0, evaluate.stdout + evaluate.stderr
    flattened = evaluate.stdout.replace("\n", " ")
    assert "5 case(s)" in flattened
    assert "heuristic_context_precision" in flattened

    # Verify independently through the API that exactly what the CLI
    # printed was actually persisted.
    runs = httpx.get(f"{live_api}/runs", timeout=5).json()
    assert len(runs) == 1
    summary = runs[0]
    assert summary["case_count"] == 5
    assert summary["status"] == "completed"
    assert summary["application_name"] == "rag-assistant"
    assert summary["dataset_name"] == "rag-support"

    run = httpx.get(f"{live_api}/runs/{summary['id']}", timeout=5).json()
    assert len(run["results"]) == 5
    # These are the real, deterministic scores produced by the example RAG
    # app's retriever against datasets/rag_support_v1.yaml (see README /
    # docs/evaluators.md for how they were derived).
    assert run["mean_score"] == pytest.approx(0.5)
    assert run["pass_rate"] == pytest.approx(0.2)
    scored_cases = {r["case_key"]: r["score"] for r in run["results"]}
    assert scored_cases["warranty-outerwear-001"] == pytest.approx(1.0)
    assert scored_cases["out-of-scope-sponsorship-001"] == pytest.approx(0.0)
    out_of_scope = next(r for r in run["results"] if r["case_key"] == "out-of-scope-sponsorship-001")
    assert out_of_scope["evidence"]["no_documents_retrieved"] is True


def test_cli_evaluate_fails_fast_on_unknown_dataset(live_api: str) -> None:
    evaluate = _run_cli(
        [
            "evaluate",
            "--app", "rag-assistant",
            "--app-version", "v1",
            "--dataset", "does-not-exist",
            "--dataset-version", "latest",
            "--adapter", "rag_app.adapter:answer",
            "--api-url", live_api,
        ]
    )
    assert evaluate.returncode != 0
    assert "Setup failed" in evaluate.stdout
