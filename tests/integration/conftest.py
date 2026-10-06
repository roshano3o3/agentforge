"""Integration test fixtures: a real FastAPI app wired to a real async
SQLAlchemy session, exercised over real HTTP semantics via httpx's ASGI
transport (no live socket, but the full FastAPI request/response and
SQLAlchemy stack runs for real -- this is not mocked).

The database is in-memory SQLite by default, or the Alembic-migrated
Postgres test DB when AGENTFORGE_TEST_DATABASE_URL is set (see
tests/conftest.py and `.\\scripts\\test.ps1 -Postgres`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from agentforge_api.db.base import Base, get_session
from agentforge_api.main import app
from agentforge_api.queue import QueueUnavailableError, get_run_queue


@pytest_asyncio.fixture
async def session_factory(test_db_url: str | None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    if test_db_url is not None:
        # Schema already built by the Alembic migrations; tables emptied by test_db_url.
        engine = create_async_engine(test_db_url, poolclass=NullPool)
    else:
        engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


class RecordingQueue:
    """Stands in for Redis/arq: records which runs the API enqueued (tests
    then execute them by calling the worker's execute_run directly), or
    fails like an unreachable Redis when `fail_with` is set."""

    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.enqueued_replays: list[str] = []
        # run id -> the W3C trace carrier the API put on the job (the worker's
        # execute_run takes it as trace_context).
        self.trace_contexts: dict[str, dict[str, str] | None] = {}
        self.fail_with: str | None = None

    async def enqueue_run(self, run_id: str, trace_context: dict[str, str] | None = None) -> None:
        if self.fail_with:
            raise QueueUnavailableError(self.fail_with)
        self.enqueued.append(run_id)
        self.trace_contexts[run_id] = trace_context

    async def enqueue_replay(self, replay_id: str, trace_context: dict[str, str] | None = None) -> None:
        if self.fail_with:
            raise QueueUnavailableError(self.fail_with)
        self.enqueued_replays.append(replay_id)
        self.trace_contexts[replay_id] = trace_context


@pytest.fixture
def queue() -> RecordingQueue:
    return RecordingQueue()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], queue: RecordingQueue
) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_run_queue] = lambda: queue
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def sample_test_cases() -> list[dict]:
    return [
        {
            "case_key": "case-1",
            "input": "What is the return window?",
            "expected_answer": "45 days",
            "expected_context": ["policy-returns-001"],
            "tags": ["policy"],
        },
        {
            "case_key": "case-2",
            "input": "Is shipping free?",
            "expected_answer": "Over $75",
            "expected_context": ["policy-shipping-002"],
            "tags": ["policy"],
        },
    ]
