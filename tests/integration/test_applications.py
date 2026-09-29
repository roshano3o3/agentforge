from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_list_applications_returns_all(client: AsyncClient) -> None:
    await client.post("/applications", json={"name": "app-one", "description": None})
    await client.post("/applications", json={"name": "app-two", "description": None})

    names = {a["name"] for a in (await client.get("/applications")).json()}
    assert names == {"app-one", "app-two"}


async def test_list_application_versions_returns_all_for_that_application(client: AsyncClient) -> None:
    app = (await client.post("/applications", json={"name": "app-one", "description": None})).json()
    other = (await client.post("/applications", json={"name": "app-two", "description": None})).json()

    await client.post(f"/applications/{app['id']}/versions", json={"version": "v1", "description": None})
    await client.post(f"/applications/{app['id']}/versions", json={"version": "v2", "description": None})
    await client.post(f"/applications/{other['id']}/versions", json={"version": "v1", "description": None})

    versions = (await client.get(f"/applications/{app['id']}/versions")).json()
    assert {v["version"] for v in versions} == {"v1", "v2"}
    assert all(v["application_id"] == app["id"] for v in versions)


async def test_application_upsert_is_idempotent(client: AsyncClient) -> None:
    first = (await client.post("/applications", json={"name": "app-one", "description": "a"})).json()
    second = (await client.post("/applications", json={"name": "app-one", "description": "different"})).json()
    assert first["id"] == second["id"]
