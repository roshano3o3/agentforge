from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_publishing_creates_version_1(client: AsyncClient, sample_test_cases: list[dict]) -> None:
    dataset = (await client.post("/datasets", json={"name": "support", "description": "d"})).json()

    resp = await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": sample_test_cases})
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert len(body["test_cases"]) == 2


async def test_republishing_creates_a_new_version_without_altering_the_old_one(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = (await client.post("/datasets", json={"name": "support", "description": "d"})).json()
    v1 = (
        await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": sample_test_cases})
    ).json()

    # Publish a second version with different content.
    new_cases = sample_test_cases + [
        {"case_key": "case-3", "input": "new question", "expected_context": [], "tags": []}
    ]
    v2 = (await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": new_cases})).json()

    assert v2["version"] == 2
    assert len(v2["test_cases"]) == 3

    # The original version's test cases must be exactly what was published
    # originally -- unchanged by the second publish.
    refetched_v1 = (await client.get("/datasets/support/versions/1")).json()
    assert refetched_v1["id"] == v1["id"]
    assert len(refetched_v1["test_cases"]) == 2
    assert {tc["case_key"] for tc in refetched_v1["test_cases"]} == {"case-1", "case-2"}


async def test_latest_resolves_to_the_highest_version(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = (await client.post("/datasets", json={"name": "support", "description": "d"})).json()
    await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": sample_test_cases})
    await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": sample_test_cases})

    latest = (await client.get("/datasets/support/versions/latest")).json()
    assert latest["version"] == 2


async def test_duplicate_case_key_within_one_publish_is_rejected(client: AsyncClient) -> None:
    dataset = (await client.post("/datasets", json={"name": "support", "description": "d"})).json()
    dupes = [
        {"case_key": "same", "input": "a", "expected_context": [], "tags": []},
        {"case_key": "same", "input": "b", "expected_context": [], "tags": []},
    ]
    resp = await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": dupes})
    assert resp.status_code == 422


async def test_dataset_name_upsert_is_idempotent(client: AsyncClient) -> None:
    first = (await client.post("/datasets", json={"name": "support", "description": "d"})).json()
    second = (await client.post("/datasets", json={"name": "support", "description": "different"})).json()
    assert first["id"] == second["id"]


async def test_unknown_dataset_version_returns_404(client: AsyncClient, sample_test_cases: list[dict]) -> None:
    dataset = (await client.post("/datasets", json={"name": "support", "description": "d"})).json()
    await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": sample_test_cases})

    resp = await client.get("/datasets/support/versions/99")
    assert resp.status_code == 404
