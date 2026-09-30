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


# -- evaluation runs ------------------------------------------------------------

RUN_IMMUTABLE = "is completed and immutable"
RESULTS_IMMUTABLE = "evaluation results are immutable once their run is completed or failed"
METRICS_IMMUTABLE = "metric scores are immutable once their run is completed or failed"


async def _executed_run(client: AsyncClient, session_factory, sample_test_cases: list[dict], *, finish: bool) -> str:
    from agentforge_api.models.evaluation import EvaluationRun, RunStatus
    from agentforge_api.services import runs as run_service
    from agentforge_worker.runner import execute_run

    await _version_id(client, sample_test_cases, publish=True)
    application = (await client.post("/applications", json={"name": "app"})).json()
    app_version = (await client.post(f"/applications/{application['id']}/versions", json={"version": "v1"})).json()
    version = (await client.get("/datasets/support/versions/latest")).json()
    resp = await client.post(
        "/runs",
        json={
            "application_id": application["id"],
            "application_version_id": app_version["id"],
            "dataset_version_id": version["id"],
            "adapter": {"type": "python", "target": "rag_app.adapter:answer"},
        },
    )
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["id"]
    if finish:
        assert await execute_run(session_factory, run_id) == "completed"
    else:
        async with session_factory() as session:
            run_service.transition(await session.get(EvaluationRun, run_id), RunStatus.running)
            await session.commit()
    return run_id


async def test_raw_update_and_delete_of_a_completed_run_are_blocked(
    client: AsyncClient, raw_engine: AsyncEngine, session_factory, sample_test_cases: list[dict]
) -> None:
    run_id = await _executed_run(client, session_factory, sample_test_cases, finish=True)

    for sql in (
        "UPDATE evaluation_runs SET status = 'running' WHERE id = :r",
        "UPDATE evaluation_runs SET aggregates = NULL WHERE id = :r",
        "DELETE FROM evaluation_runs WHERE id = :r",
    ):
        with pytest.raises(DBAPIError, match=RUN_IMMUTABLE):
            await _execute(raw_engine, sql, r=run_id)


async def test_raw_writes_to_a_completed_runs_results_and_metrics_are_blocked(
    client: AsyncClient, raw_engine: AsyncEngine, session_factory, sample_test_cases: list[dict]
) -> None:
    run_id = await _executed_run(client, session_factory, sample_test_cases, finish=True)
    async with raw_engine.connect() as conn:
        result_id, test_case_id = (
            await conn.execute(
                text("SELECT id, test_case_id FROM evaluation_results WHERE evaluation_run_id = :r LIMIT 1"),
                {"r": run_id},
            )
        ).one()

    with pytest.raises(DBAPIError, match=RESULTS_IMMUTABLE):
        await _execute(raw_engine, "UPDATE evaluation_results SET passed = true WHERE id = :i", i=result_id)
    with pytest.raises(DBAPIError, match=RESULTS_IMMUTABLE):
        await _execute(raw_engine, "DELETE FROM evaluation_results WHERE id = :i", i=result_id)
    with pytest.raises(DBAPIError, match=RESULTS_IMMUTABLE):
        await _execute(
            raw_engine,
            "INSERT INTO evaluation_results (id, evaluation_run_id, test_case_id, retrieved_doc_ids, citations, "
            "latency_ms, status, created_at) VALUES ('forged', :r, :t, CAST('[]' AS JSON), CAST('[]' AS JSON), "
            "1, 'ok', now())",
            r=run_id, t=test_case_id,
        )
    with pytest.raises(DBAPIError, match=METRICS_IMMUTABLE):
        await _execute(raw_engine, "UPDATE metric_scores SET score = 1.0 WHERE evaluation_result_id = :i", i=result_id)
    with pytest.raises(DBAPIError, match=METRICS_IMMUTABLE):
        await _execute(
            raw_engine,
            "INSERT INTO metric_scores (id, evaluation_result_id, evaluator_name, evaluator_version, reason, "
            "evidence, labels, created_at) VALUES ('forged', :i, 'exact_match', '1.0.0', 'forged', "
            "CAST('{}' AS JSON), CAST('[]' AS JSON), now())",
            i=result_id,
        )


async def test_raw_writes_to_a_running_run_are_allowed(
    client: AsyncClient, raw_engine: AsyncEngine, session_factory, sample_test_cases: list[dict]
) -> None:
    # Control: the triggers only freeze finished runs -- the worker must be
    # able to write while a run is in progress.
    run_id = await _executed_run(client, session_factory, sample_test_cases, finish=False)
    assert await _execute(raw_engine, "UPDATE evaluation_runs SET environment = 'x' WHERE id = :r", r=run_id) == 1
