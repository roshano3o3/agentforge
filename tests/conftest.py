"""Shared test-database selection.

By default the integration and e2e tests use a throwaway SQLite database
whose schema comes from `Base.metadata.create_all` (fast, no Docker).

Set AGENTFORGE_TEST_DATABASE_URL (e.g. via `.\\scripts\\test.ps1 -Postgres`)
to run the same tests against a real Postgres database instead. In that mode
the schema is built by the real Alembic migrations -- so the DB-level
immutability triggers exist, unlike with create_all -- once per session, and
every table is TRUNCATEd before each test. TRUNCATE does not fire row-level
triggers, so published test_cases can still be cleared between tests.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import agentforge_api.models  # noqa: F401 -- registers tables on Base.metadata
from agentforge_api.db.base import Base

API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
TEST_DATABASE_URL = os.environ.get("AGENTFORGE_TEST_DATABASE_URL") or None


@pytest.fixture(scope="session")
def migrated_test_db_url() -> str | None:
    """The migrated external test DB URL, or None in the default SQLite mode."""
    if TEST_DATABASE_URL is None:
        return None
    # Every test TRUNCATEs every table: refuse anything that isn't obviously a
    # disposable test database, so this can never wipe the dev DB.
    database = make_url(TEST_DATABASE_URL).database or ""
    if not database.endswith("_test"):
        pytest.exit(
            f"AGENTFORGE_TEST_DATABASE_URL must name a database ending in '_test' (got '{database}')",
            returncode=2,
        )
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env={**os.environ, "AGENTFORGE_DATABASE_URL": TEST_DATABASE_URL},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert migrate.returncode == 0, migrate.stdout + migrate.stderr
    return TEST_DATABASE_URL


async def _truncate_all_tables(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    finally:
        await engine.dispose()


@pytest.fixture
def test_db_url(migrated_test_db_url: str | None) -> str | None:
    """Per test: the external test DB URL with every table emptied, or None
    in the default SQLite mode (each fixture then builds its own SQLite DB)."""
    if migrated_test_db_url is not None:
        asyncio.run(_truncate_all_tables(migrated_test_db_url))
    return migrated_test_db_url
