from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _create_dataset(client: AsyncClient, name: str = "support") -> dict:
    return (await client.post("/datasets", json={"name": name, "description": "d"})).json()


async def _create_draft(client: AsyncClient, dataset_id: str, test_cases: list[dict]) -> dict:
    resp = await client.post(f"/datasets/{dataset_id}/versions", json={"test_cases": test_cases})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _publish(client: AsyncClient, name: str, version: int) -> dict:
    resp = await client.post(f"/datasets/{name}/versions/{version}/publish")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_creating_a_version_starts_it_as_an_editable_draft(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = await _create_dataset(client)
    draft = await _create_draft(client, dataset["id"], sample_test_cases)

    assert draft["version"] == 1
    assert draft["status"] == "draft"
    assert draft["published_at"] is None
    assert len(draft["test_cases"]) == 2


async def test_draft_can_be_created_empty(client: AsyncClient) -> None:
    dataset = await _create_dataset(client)
    draft = await _create_draft(client, dataset["id"], [])
    assert draft["status"] == "draft"
    assert draft["test_cases"] == []


async def test_publishing_freezes_the_version(client: AsyncClient, sample_test_cases: list[dict]) -> None:
    dataset = await _create_dataset(client)
    draft = await _create_draft(client, dataset["id"], sample_test_cases)

    published = await _publish(client, "support", draft["version"])
    assert published["status"] == "published"
    assert published["published_at"] is not None


async def test_publishing_a_version_with_zero_test_cases_is_rejected(client: AsyncClient) -> None:
    dataset = await _create_dataset(client)
    draft = await _create_draft(client, dataset["id"], [])

    resp = await client.post(f"/datasets/support/versions/{draft['version']}/publish")
    assert resp.status_code == 400


async def test_publishing_an_already_published_version_is_rejected(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = await _create_dataset(client)
    draft = await _create_draft(client, dataset["id"], sample_test_cases)
    await _publish(client, "support", draft["version"])

    resp = await client.post(f"/datasets/support/versions/{draft['version']}/publish")
    assert resp.status_code == 409


async def test_patch_edits_a_draft_version_in_place(client: AsyncClient, sample_test_cases: list[dict]) -> None:
    dataset = await _create_dataset(client)
    draft = await _create_draft(client, dataset["id"], sample_test_cases)

    edited = [
        {"case_key": "case-1", "input": "an edited question", "expected_context": [], "tags": []},
        # case-2 dropped, case-3 added: PATCH replaces the whole set.
        {"case_key": "case-3", "input": "a brand new case", "expected_context": [], "tags": []},
    ]
    resp = await client.patch(f"/datasets/support/versions/{draft['version']}", json={"test_cases": edited})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    by_key = {tc["case_key"]: tc for tc in body["test_cases"]}
    assert set(by_key) == {"case-1", "case-3"}
    assert by_key["case-1"]["input"] == "an edited question"


async def test_patch_on_a_published_version_is_rejected_with_409_same_code_path(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = await _create_dataset(client)
    draft = await _create_draft(client, dataset["id"], sample_test_cases)
    await _publish(client, "support", draft["version"])

    resp = await client.patch(
        f"/datasets/support/versions/{draft['version']}",
        json={"test_cases": [{"case_key": "case-1", "input": "edited!", "expected_context": [], "tags": []}]},
    )
    assert resp.status_code == 409
    assert "immutable" in resp.json()["detail"].lower()

    # The rejected attempt provably didn't change anything.
    unchanged = (await client.get(f"/datasets/support/versions/{draft['version']}")).json()
    assert unchanged["test_cases"][0]["input"] == sample_test_cases[0]["input"]


async def test_new_draft_from_published_version_copies_test_cases_into_a_fresh_draft(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = await _create_dataset(client)
    v1 = await _create_draft(client, dataset["id"], sample_test_cases)
    await _publish(client, "support", v1["version"])

    resp = await client.post(f"/datasets/support/versions/{v1['version']}/new-draft")
    assert resp.status_code == 200, resp.text
    v2 = resp.json()

    assert v2["version"] == 2
    assert v2["status"] == "draft"
    assert {tc["case_key"] for tc in v2["test_cases"]} == {"case-1", "case-2"}

    # v2 is independently editable -- editing it never touches v1.
    await client.patch(
        f"/datasets/support/versions/{v2['version']}",
        json={
            "test_cases": [{"case_key": "case-1", "input": "changed in v2 only", "expected_context": [], "tags": []}]
        },
    )
    v1_refetched = (await client.get("/datasets/support/versions/1")).json()
    assert v1_refetched["test_cases"][0]["input"] == sample_test_cases[0]["input"]


async def test_latest_resolves_to_highest_published_version_only(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = await _create_dataset(client)
    v1 = await _create_draft(client, dataset["id"], sample_test_cases)
    await _publish(client, "support", v1["version"])
    # v2 stays a draft.
    await _create_draft(client, dataset["id"], sample_test_cases)

    latest = (await client.get("/datasets/support/versions/latest")).json()
    assert latest["version"] == 1
    assert latest["status"] == "published"


async def test_latest_404s_when_no_published_version_exists(client: AsyncClient, sample_test_cases: list[dict]) -> None:
    dataset = await _create_dataset(client)
    await _create_draft(client, dataset["id"], sample_test_cases)  # draft only, never published

    resp = await client.get("/datasets/support/versions/latest")
    assert resp.status_code == 404


async def test_duplicate_case_key_within_one_request_is_rejected(client: AsyncClient) -> None:
    dataset = await _create_dataset(client)
    dupes = [
        {"case_key": "same", "input": "a", "expected_context": [], "tags": []},
        {"case_key": "same", "input": "b", "expected_context": [], "tags": []},
    ]
    resp = await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": dupes})
    assert resp.status_code == 422


async def test_dataset_name_upsert_is_idempotent(client: AsyncClient) -> None:
    first = await _create_dataset(client)
    second = (await client.post("/datasets", json={"name": "support", "description": "different"})).json()
    assert first["id"] == second["id"]


async def test_unknown_dataset_version_returns_404(client: AsyncClient, sample_test_cases: list[dict]) -> None:
    dataset = await _create_dataset(client)
    await _create_draft(client, dataset["id"], sample_test_cases)

    resp = await client.get("/datasets/support/versions/99")
    assert resp.status_code == 404


async def test_list_dataset_versions_returns_all_versions_newest_first(
    client: AsyncClient, sample_test_cases: list[dict]
) -> None:
    dataset = await _create_dataset(client)
    await _create_draft(client, dataset["id"], sample_test_cases)
    await _create_draft(client, dataset["id"], sample_test_cases)

    versions = (await client.get("/datasets/support/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]


async def test_get_dataset_by_name(client: AsyncClient) -> None:
    created = await _create_dataset(client)
    fetched = (await client.get("/datasets/support")).json()
    assert fetched["id"] == created["id"]

    missing = await client.get("/datasets/does-not-exist")
    assert missing.status_code == 404


async def test_list_datasets_returns_all(client: AsyncClient) -> None:
    await client.post("/datasets", json={"name": "one", "description": None})
    await client.post("/datasets", json={"name": "two", "description": None})

    names = {d["name"] for d in (await client.get("/datasets")).json()}
    assert names == {"one", "two"}
