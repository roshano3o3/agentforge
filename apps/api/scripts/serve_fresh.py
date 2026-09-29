"""Create the schema (if needed) against AGENTFORGE_DATABASE_URL, then serve.

Used by Playwright (apps/web/playwright.config.ts) to bring up a clean API
instance -- pointed at its own temp SQLite file, never the shared dev DB --
before running browser tests, and torn down again afterward. Not used by
the normal dev workflow (that's scripts/dev-api.ps1 + scripts/db-migrate.ps1
with Alembic).
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

import agentforge_api.models  # noqa: F401 -- registers tables on Base.metadata
from agentforge_api.db.base import Base, engine


async def _create_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


if __name__ == "__main__":
    asyncio.run(_create_schema())
    uvicorn.run(
        "agentforge_api.main:app",
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8010")),
    )
