"""Integration test fixtures: a real FastAPI app wired to a real (in-memory
SQLite) async SQLAlchemy session, exercised over real HTTP semantics via
httpx's ASGI transport (no live socket, but the full FastAPI request/response
and SQLAlchemy stack runs for real -- this is not mocked).

SQLite stands in for Postgres here because Docker isn't available in this
environment (see README "Development without Docker"). The schema and ORM
code are identical either way; Alembic's migration path against real
Postgres is documented separately and was verified manually.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from agentforge_api.db.base import Base, get_session
from agentforge_api.main import app


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
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


@pytest_asyncio.fixture
async def client(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
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
