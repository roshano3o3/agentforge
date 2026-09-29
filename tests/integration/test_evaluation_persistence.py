from __future__ import annotations

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _publish_run_setup(client: AsyncClient, test_cases: list[dict]) -> dict:
    application = (await client.post("/applications", json={"name": "app", "description": None})).json()
    app_version = (
        await client.post(f"/applications/{application['id']}/versions", json={"version": "v1", "description": None})
    ).json()
    dataset = (await client.post("/datasets", json={"name": "ds", "description": None})).json()
    dataset_version = (
        await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": test_cases})
    ).json()
    return {
        "application_id": application["id"],
        "application_version_id": app_version["id"],
        "dataset_version_id": dataset_version["id"],
        "test_case_ids": [tc["id"] for tc in dataset_version["test_cases"]],
    }


async def test_run_lifecycle_persists_results_and_computes_aggregates(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    setup = await _publish_run_setup(client, sample_test_cases)

    run = (
        await client.post(
            "/runs",
            json={
                "application_id": setup["application_id"],
                "application_version_id": setup["application_version_id"],
                "dataset_version_id": setup["dataset_version_id"],
                "provider_type": "local-deterministic",
                "evaluator_name": "heuristic_context_precision",
                "evaluator_version": "1.0.0",
                "environment": "test",
                "threshold": 0.7,
                "git_commit_sha": "abc123",
            },
        )
    ).json()
    assert run["status"] == "running"
    assert run["case_count"] == 0

    results = [
        {
            "test_case_id": setup["test_case_ids"][0],
            "retrieved_doc_ids": ["policy-returns-001"],
            "output_answer": "45 days",
            "score": 1.0,
            "passed": True,
            "evidence": {"formula": "test"},
            "latency_ms": 5,
            "status": "ok",
        },
        {
            "test_case_id": setup["test_case_ids"][1],
            "retrieved_doc_ids": ["policy-shipping-002", "policy-other"],
            "output_answer": "maybe",
            "score": 0.5,
            "passed": False,
            "evidence": {"formula": "test"},
            "latency_ms": 15,
            "status": "ok",
        },
    ]
    submit_resp = await client.post(f"/runs/{run['id']}/results", json={"results": results})
    assert submit_resp.status_code == 200

    complete_resp = await client.post(f"/runs/{run['id']}/complete", json={"status": "completed"})
    completed_run = complete_resp.json()

    assert completed_run["status"] == "completed"
    assert completed_run["case_count"] == 2
    assert completed_run["mean_score"] == pytest.approx(0.75)
    assert completed_run["pass_rate"] == pytest.approx(0.5)
    assert completed_run["avg_latency_ms"] == pytest.approx(10.0)
    assert completed_run["git_commit_sha"] == "abc123"
    assert completed_run["completed_at"] is not None

    # Immutability: fetching again returns exactly the same persisted data.
    #
    # Timestamps are compared separately (parsed, not as raw strings):
    # SQLite -- the local dev/test fallback engine used here because Docker
    # isn't available in this environment -- doesn't preserve the "Z"
    # UTC-offset suffix across a write+re-read the way Postgres's
    # timezone-aware column does, so the same instant can come back with a
    # differently-formatted (but equal) ISO string. This is a serialization
    # quirk of the SQLite fallback, not a data-integrity bug.
    refetched = (await client.get(f"/runs/{run['id']}")).json()
    for field in ("created_at", "completed_at"):
        assert _parse_iso(refetched[field]) == _parse_iso(completed_run[field])
        del refetched[field]
        del completed_run[field]
    assert refetched == completed_run


async def test_submitting_results_after_completion_is_rejected(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    setup = await _publish_run_setup(client, sample_test_cases)
    run = (
        await client.post(
            "/runs",
            json={
                "application_id": setup["application_id"],
                "application_version_id": setup["application_version_id"],
                "dataset_version_id": setup["dataset_version_id"],
                "provider_type": "local-deterministic",
                "evaluator_name": "heuristic_context_precision",
                "evaluator_version": "1.0.0",
            },
        )
    ).json()
    await client.post(f"/runs/{run['id']}/complete", json={"status": "completed"})

    late_submit = await client.post(
        f"/runs/{run['id']}/results",
        json={
            "results": [
                {
                    "test_case_id": setup["test_case_ids"][0],
                    "retrieved_doc_ids": [],
                    "score": 0.0,
                    "evidence": {},
                    "latency_ms": 1,
                    "status": "ok",
                }
            ]
        },
    )
    assert late_submit.status_code == 409


async def test_result_for_test_case_outside_runs_dataset_version_is_rejected(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    setup = await _publish_run_setup(client, sample_test_cases)
    run = (
        await client.post(
            "/runs",
            json={
                "application_id": setup["application_id"],
                "application_version_id": setup["application_version_id"],
                "dataset_version_id": setup["dataset_version_id"],
                "provider_type": "local-deterministic",
                "evaluator_name": "heuristic_context_precision",
                "evaluator_version": "1.0.0",
            },
        )
    ).json()

    resp = await client.post(
        f"/runs/{run['id']}/results",
        json={
            "results": [
                {
                    "test_case_id": "not-a-real-test-case-id",
                    "retrieved_doc_ids": [],
                    "score": 0.0,
                    "evidence": {},
                    "latency_ms": 1,
                    "status": "ok",
                }
            ]
        },
    )
    assert resp.status_code == 400


async def test_list_runs_returns_summaries_most_recent_first(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    setup = await _publish_run_setup(client, sample_test_cases)
    for _ in range(2):
        run = (
            await client.post(
                "/runs",
                json={
                    "application_id": setup["application_id"],
                    "application_version_id": setup["application_version_id"],
                    "dataset_version_id": setup["dataset_version_id"],
                    "provider_type": "local-deterministic",
                    "evaluator_name": "heuristic_context_precision",
                    "evaluator_version": "1.0.0",
                },
            )
        ).json()
        await client.post(f"/runs/{run['id']}/complete", json={"status": "completed"})

    runs = (await client.get("/runs")).json()
    assert len(runs) == 2
    assert runs[0]["created_at"] >= runs[1]["created_at"]
