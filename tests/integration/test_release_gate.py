"""Baselines, regression and the release gate through the real API, with
runs executed by the worker's real `execute_run`: the invoice agent's v1 as
the production baseline, then a fresh v1 run (PASSES) and a v2 run (FAILS)
as candidates, under the repo's own agentforge.yaml policy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from agentforge_api.models.evaluation import EvaluationResult, EvaluationRun, RunStatus
from agentforge_api.services import runs as run_service
from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_cli.release_io import load_config
from agentforge_worker.runner import execute_run

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[2]
DATASET = REPO / "datasets" / "invoice_agent_v1.yaml"
POLICY = load_config(REPO / "agentforge.yaml").policy.to_dict()  # type: ignore[union-attr]
V1 = "invoice_agent.adapter:answer_v1"
V2 = "invoice_agent.adapter:answer_v2"


async def _publish(client: AsyncClient) -> str:
    """Publish the invoice dataset (as a new version) and return its id."""
    name, _desc, cases, default_evaluators = validate_dataset_file(DATASET)
    dataset = (await client.post("/datasets", json={"name": name})).json()
    version = (
        await client.post(
            f"/datasets/{dataset['id']}/versions",
            json={"test_cases": [tc.model_dump() for tc in cases], "default_evaluators": default_evaluators},
        )
    ).json()
    resp = await client.post(f"/datasets/{name}/versions/{version['version']}/publish")
    assert resp.status_code == 200, resp.text
    return version["id"]


@pytest_asyncio.fixture
async def env(client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]) -> dict:
    application = (await client.post("/applications", json={"name": "invoice-agent"})).json()
    versions = {
        v: (await client.post(f"/applications/{application['id']}/versions", json={"version": v})).json()["id"]
        for v in ("v1", "v2")
    }
    dataset_version_id = await _publish(client)

    async def run(app_version: str, target: str, dataset_version: str = dataset_version_id) -> str:
        resp = await client.post(
            "/runs",
            json={
                "application_id": application["id"],
                "application_version_id": versions[app_version],
                "dataset_version_id": dataset_version,
                "adapter": {"type": "python", "target": target},
            },
        )
        assert resp.status_code == 202, resp.text
        assert await execute_run(session_factory, resp.json()["id"]) == "completed"
        return resp.json()["id"]

    return {"application": application, "run": run, "dataset_version_id": dataset_version_id}


async def _gate(client: AsyncClient, candidate: str, baseline: str = "production", policy: dict | None = None):
    return await client.post(
        "/release-decisions",
        json={"candidate_run_id": candidate, "baseline": baseline, "policy": policy or POLICY},
    )


# -- baselines ----------------------------------------------------------------------


async def test_baseline_is_a_pointer_set_and_repointed_without_copying(
    client: AsyncClient, env: dict, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    first = await env["run"]("v1", V1)
    second = await env["run"]("v1", V1)
    async with session_factory() as session:
        results_before = await session.scalar(select(func.count()).select_from(EvaluationResult))

    resp = await client.put("/baselines", json={"run_id": first, "environment": "production"})
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["run_id"] == first and row["environment"] == "production"
    assert row["application_name"] == "invoice-agent" and row["pass_rate"] == 1.0
    assert row["dataset_name"] == "invoice-agent" and row["application_version"] == "v1"

    resp = await client.put("/baselines", json={"run_id": second, "environment": "production"})
    assert resp.json()["run_id"] == second
    await client.put("/baselines", json={"run_id": first, "environment": "staging"})

    listed = (await client.get("/baselines", params={"application": "invoice-agent"})).json()
    assert [(b["environment"], b["run_id"]) for b in listed] == [("production", second), ("staging", first)]
    assert (await client.get("/baselines", params={"environment": "staging"})).json()[0]["run_id"] == first
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EvaluationResult)) == results_before


async def test_baseline_must_be_a_completed_run(
    client: AsyncClient, env: dict, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    assert (await client.put("/baselines", json={"run_id": "nope", "environment": "production"})).status_code == 404
    # A run that's still running can't be a baseline.
    run_id = await env["run"]("v1", V1)
    resp = await client.post(
        "/runs",
        json={
            "application_id": env["application"]["id"],
            "application_version_id": (await client.get(f"/runs/{run_id}")).json()["application_version_id"],
            "dataset_version_id": env["dataset_version_id"],
            "adapter": {"type": "python", "target": V1},
        },
    )
    pending = resp.json()["id"]
    async with session_factory() as session:
        row = await session.get(EvaluationRun, pending)
        assert row is not None
        run_service.transition(row, RunStatus.running)
        await session.commit()
    resp = await client.put("/baselines", json={"run_id": pending, "environment": "production"})
    assert resp.status_code == 400
    assert "is running; only completed runs" in resp.json()["detail"]


# -- the gate -----------------------------------------------------------------------


async def test_v1_baseline_and_v1_candidate_passes(client: AsyncClient, env: dict) -> None:
    baseline = await env["run"]("v1", V1)
    candidate = await env["run"]("v1", V1)
    await client.put("/baselines", json={"run_id": baseline, "environment": "production"})

    resp = await _gate(client, candidate)
    assert resp.status_code == 201, resp.text
    decision = resp.json()
    assert decision["passed"] is True
    assert decision["baseline_run_id"] == baseline and decision["baseline_ref"] == "production"
    assert decision["policy"] == POLICY
    failed = [c for c in decision["checks"] if not c["passed"]]
    assert failed == []
    by_metric = {(c["kind"], c["metric"]): c for c in decision["checks"]}
    assert set(by_metric) == {
        ("minimum", "pass_rate"),
        ("minimum", "approval_required.pass_rate"),
        ("maximum", "error_rate"),
        ("maximum", "p95_latency_ms"),
        ("regression", "pass_rate"),
        ("regression", "tool_selection.mean_score"),
        ("regression", "p95_latency_ms"),
        ("cases", "newly_failing[tag=critical]"),
    }
    pr = by_metric[("regression", "pass_rate")]
    assert (pr["baseline"], pr["candidate"], pr["delta"], pr["threshold"]) == (1.0, 1.0, 0.0, 0.02)
    assert decision["regression"]["case_counts"] == {
        "newly_failing": 0,
        "fixed": 0,
        "still_failing": 0,
        "still_passing": 10,
    }
    # Persisted, and readable back unchanged (created_at aside: the known SQLite
    # quirk drops the UTC suffix on re-read; Postgres doesn't).
    stored = (await client.get(f"/release-decisions/{decision['id']}")).json()
    assert {k: v for k, v in stored.items() if k != "created_at"} == {
        k: v for k, v in decision.items() if k != "created_at"
    }


async def test_v1_baseline_and_v2_candidate_fails_on_the_right_checks(client: AsyncClient, env: dict) -> None:
    baseline = await env["run"]("v1", V1)
    candidate = await env["run"]("v2", V2)
    await client.put("/baselines", json={"run_id": baseline, "environment": "production"})

    decision = (await _gate(client, candidate)).json()
    assert decision["passed"] is False
    failed = {(c["kind"], c["metric"]): c for c in decision["checks"] if not c["passed"]}
    assert set(failed) == {
        ("minimum", "pass_rate"),
        ("minimum", "approval_required.pass_rate"),
        ("regression", "pass_rate"),
        ("regression", "tool_selection.mean_score"),
        ("cases", "newly_failing[tag=critical]"),
    }
    assert failed[("minimum", "pass_rate")]["reason"] == "0.4 < 0.9"
    assert failed[("minimum", "approval_required.pass_rate")]["candidate"] == 0.0
    drop = failed[("regression", "pass_rate")]
    assert drop["delta"] == pytest.approx(-0.6) and drop["delta_pct"] == pytest.approx(-60.0)
    assert drop["reason"] == "drop 0.6 (1 -> 0.4) exceeds 0.02"
    ts = failed[("regression", "tool_selection.mean_score")]
    assert ts["candidate"] == pytest.approx(0.78)
    assert failed[("cases", "newly_failing[tag=critical]")]["reason"] == (
        "newly failing 'critical' case(s): refund-over-limit-001, refund-small-001, void-duplicate-001"
    )
    # error_rate and latency still pass: v2's problems are trajectory, not crashes.
    passing = {c["metric"] for c in decision["checks"] if c["passed"]}
    assert passing == {"error_rate", "p95_latency_ms"}

    counts = decision["regression"]["case_counts"]
    assert counts == {"newly_failing": 6, "fixed": 0, "still_failing": 0, "still_passing": 4}

    # Same comparison from the regression endpoint.
    report = (
        await client.get("/regression", params={"baseline_run_id": baseline, "candidate_run_id": candidate})
    ).json()
    assert report == decision["regression"]
    newly = {c["case_key"]: c for c in report["cases"]["newly_failing"]}
    assert newly["refund-small-001"]["candidate_failed_evaluators"] == [
        "approval_required",
        "sequence_order",
        "tool_args",
        "tool_selection",
    ]
    approval = next(m for m in report["metrics"] if m["name"] == "approval_required")
    assert approval["comparable"] is True
    assert (approval["pass_rate"]["baseline"], approval["pass_rate"]["candidate"]) == (1.0, 0.0)

    listed = (await client.get("/release-decisions", params={"candidate_run_id": candidate})).json()
    assert [d["id"] for d in listed] == [decision["id"]]


async def test_runs_of_different_dataset_versions_are_not_compared(client: AsyncClient, env: dict) -> None:
    baseline = await env["run"]("v1", V1)
    other_version = await _publish(client)  # same file, published again: a different dataset version
    candidate = await env["run"]("v1", V1, other_version)
    await client.put("/baselines", json={"run_id": baseline, "environment": "production"})

    resp = await client.get("/regression", params={"baseline_run_id": baseline, "candidate_run_id": candidate})
    assert resp.status_code == 400
    assert "different dataset versions" in resp.json()["detail"]
    resp = await _gate(client, candidate)
    assert resp.status_code == 400
    assert "different dataset versions" in resp.json()["detail"]
    assert (await client.get("/release-decisions")).json() == []


async def test_gate_input_errors(client: AsyncClient, env: dict) -> None:
    candidate = await env["run"]("v1", V1)
    # No production baseline yet.
    resp = await _gate(client, candidate)
    assert resp.status_code == 404
    assert "no baseline set for application 'invoice-agent' in environment 'production'" in resp.json()["detail"]
    # A run id works as the baseline too.
    assert (await _gate(client, candidate, baseline=candidate)).json()["passed"] is True
    # The API validates the policy itself, whatever the client did.
    resp = await _gate(client, candidate, baseline=candidate, policy={"maximums": {"pass_rate": 0.5}})
    assert resp.status_code == 422
    assert "invalid release policy: maximums.pass_rate" in resp.json()["detail"]
    resp = await _gate(client, "missing-run", baseline=candidate)
    assert resp.status_code == 404


# -- immutability (Postgres: the migration's triggers) -----------------------------------


@pytest_asyncio.fixture
async def raw_engine(test_db_url: str | None) -> AsyncIterator[AsyncEngine]:
    if test_db_url is None:
        pytest.skip("needs the Alembic-migrated test DB (AGENTFORGE_TEST_DATABASE_URL); create_all has no triggers")
    engine = create_async_engine(test_db_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_release_decisions_are_immutable_in_the_database(
    client: AsyncClient, env: dict, raw_engine: AsyncEngine
) -> None:
    baseline = await env["run"]("v1", V1)
    candidate = await env["run"]("v2", V2)
    decision = (await _gate(client, candidate, baseline=baseline)).json()
    assert decision["passed"] is False

    for sql in (
        "UPDATE release_decisions SET passed = true WHERE id = :d",
        "UPDATE release_decisions SET checks = CAST('[]' AS JSON) WHERE id = :d",
        "DELETE FROM release_decisions WHERE id = :d",
    ):
        with pytest.raises(DBAPIError, match="release decisions are immutable"):
            async with raw_engine.begin() as conn:
                await conn.execute(text(sql), {"d": decision["id"]})
    assert (await client.get(f"/release-decisions/{decision['id']}")).json()["passed"] is False


async def test_baseline_pointer_is_checked_in_the_database(
    client: AsyncClient, env: dict, raw_engine: AsyncEngine, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    good = await env["run"]("v1", V1)
    await client.put("/baselines", json={"run_id": good, "environment": "production"})
    # A pending run of the same application, and a completed run of another application.
    pending = (
        await client.post(
            "/runs",
            json={
                "application_id": env["application"]["id"],
                "application_version_id": (await client.get(f"/runs/{good}")).json()["application_version_id"],
                "dataset_version_id": env["dataset_version_id"],
                "adapter": {"type": "python", "target": V1},
            },
        )
    ).json()["id"]
    other_app = (await client.post("/applications", json={"name": "other"})).json()
    other_version = (await client.post(f"/applications/{other_app['id']}/versions", json={"version": "x"})).json()
    resp = await client.post(
        "/runs",
        json={
            "application_id": other_app["id"],
            "application_version_id": other_version["id"],
            "dataset_version_id": env["dataset_version_id"],
            "adapter": {"type": "python", "target": V1},
        },
    )
    foreign = resp.json()["id"]
    assert await execute_run(session_factory, foreign) == "completed"

    for run_id in (pending, foreign):
        with pytest.raises(DBAPIError, match="a baseline must point to a completed run of the same application"):
            async with raw_engine.begin() as conn:
                await conn.execute(
                    text("UPDATE baselines SET run_id = :r WHERE environment = 'production'"), {"r": run_id}
                )
