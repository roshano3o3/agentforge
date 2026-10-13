"""Health check for one worker process's dependencies, for container probes:

    python -m agentforge_worker.healthcheck     # exit 0: Redis and Postgres both answered; exit 1 otherwise

The worker serves no HTTP, and arq's own `arq --check` reads a health key shared by every worker on the queue,
so it can't say which pod is unhealthy. This checks what *this* pod needs to do its job: the queue it consumes
(Redis PING) and the database it writes results to (`SELECT 1`), each within a short timeout, using the same
settings the worker uses (AGENTFORGE_REDIS_URL, AGENTFORGE_DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import sys

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agentforge_api.config import get_settings

TIMEOUT_S = 3.0


async def check() -> list[str]:
    """The problems found (empty = healthy)."""
    settings = get_settings()
    problems: list[str] = []
    try:
        redis = Redis.from_url(settings.redis_url, socket_connect_timeout=TIMEOUT_S, socket_timeout=TIMEOUT_S)
        try:
            await asyncio.wait_for(redis.ping(), TIMEOUT_S)
        finally:
            await redis.aclose()
    except Exception as exc:  # noqa: BLE001 - any failure is "unhealthy", reported with its reason
        problems.append(f"redis: {type(exc).__name__}: {exc}")
    engine = create_async_engine(settings.database_url, pool_pre_ping=False)
    try:
        async with asyncio.timeout(TIMEOUT_S), engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"database: {type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()
    return problems


def main() -> None:
    problems = asyncio.run(check())
    for p in problems:
        print(p, file=sys.stderr)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
