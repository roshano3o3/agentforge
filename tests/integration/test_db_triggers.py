"""The DB-level immutability triggers, exercised with raw SQL that bypasses
the API and ORM entirely.

Only meaningful against the Alembic-migrated test DB (AGENTFORGE_TEST_DATABASE_URL,
e.g. `.\\scripts\\test.ps1 -Postgres`): the default SQLite mode builds its schema
with create_all, which has no triggers, so these tests are skipped there.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.asyncio

TEST_CASES_IMMUTABLE = "test_cases are immutable once their dataset_version is published"


@pytest_asyncio.fixture
async def raw_engine(test_db_url: str | None) -> AsyncIterator[AsyncEngine]:
    if test_db_url is None:
        pytest.skip("needs the Alembic-migrated test DB (AGENTFORGE_TEST_DATABASE_URL); create_all has no triggers")
    engine = create_async_engine(test_db_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _version_id(client: AsyncClient, sample_test_cases: list[dict], *, publish: bool) -> str:
    dataset = (await client.post("/datasets", json={"name": "support", "description": "d"})).json()
    draft = (await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": sample_test_cases})).json()
    if publish:
        resp = await client.post(f"/datasets/support/versions/{draft['version']}/publish")
        assert resp.status_code == 200, resp.text
    return draft["id"]


async def _execute(engine: AsyncEngine, sql: str, **params: object) -> int:
    async with engine.begin() as conn:
        return (await conn.execute(text(sql), params)).rowcount


async def test_raw_update_on_published_test_cases_is_blocked(
    client: AsyncClient, raw_engine: AsyncEngine, sample_test_cases: list[dict]
) -> None:
    version_id = await _version_id(client, sample_test_cases, publish=True)

    with pytest.raises(DBAPIError, match=TEST_CASES_IMMUTABLE):
        await _execute(raw_engine, "UPDATE test_cases SET input = 'tampered' WHERE dataset_version_id = :v", v=version_id)

    async with raw_engine.connect() as conn:
        inputs = (await conn.execute(text("SELECT input FROM test_cases WHERE dataset_version_id = :v"), {"v": version_id})).scalars().all()
    assert sorted(inputs) == sorted(tc["input"] for tc in sample_test_cases)


async def test_raw_insert_and_delete_on_published_test_cases_are_blocked(
    client: AsyncClient, raw_engine: AsyncEngine, sample_test_cases: list[dict]
) -> None:
    version_id = await _version_id(client, sample_test_cases, publish=True)

    with pytest.raises(DBAPIError, match=TEST_CASES_IMMUTABLE):
        await _execute(
            raw_engine,
            "INSERT INTO test_cases (id, dataset_version_id, case_key, input, expected_context, tags) "
            "VALUES ('raw-insert', :v, 'raw', 'x', CAST('[]' AS JSON), CAST('[]' AS JSON))",
            v=version_id,
        )
    with pytest.raises(DBAPIError, match=TEST_CASES_IMMUTABLE):
        await _execute(raw_engine, "DELETE FROM test_cases WHERE dataset_version_id = :v", v=version_id)


async def test_raw_unpublish_is_blocked(
    client: AsyncClient, raw_engine: AsyncEngine, sample_test_cases: list[dict]
) -> None:
    version_id = await _version_id(client, sample_test_cases, publish=True)

    with pytest.raises(DBAPIError, match="a published dataset_version can never move back to draft"):
        await _execute(raw_engine, "UPDATE dataset_versions SET status = 'draft' WHERE id = :v", v=version_id)


async def test_raw_update_on_a_draft_is_allowed(
    client: AsyncClient, raw_engine: AsyncEngine, sample_test_cases: list[dict]
) -> None:
    # Control: the trigger blocks published versions only, not all writes.
    version_id = await _version_id(client, sample_test_cases, publish=False)

    updated = await _execute(raw_engine, "UPDATE test_cases SET input = 'edited' WHERE dataset_version_id = :v", v=version_id)
    assert updated == len(sample_test_cases)
