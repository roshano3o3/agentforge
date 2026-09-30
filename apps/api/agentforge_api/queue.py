"""The run queue: the API only enqueues; the worker (Docker only) executes.

`get_run_queue` is a FastAPI dependency so tests can swap in a recorder and
drive the worker function directly, while the real app talks to Redis via arq.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from agentforge_api.config import get_settings

EXECUTE_RUN_JOB = "execute_run"


class QueueUnavailableError(RuntimeError):
    pass


class RunQueue(Protocol):
    async def enqueue_run(self, run_id: str) -> None: ...


class ArqRunQueue:
    def __init__(self, redis_url: str) -> None:
        # Fail fast (one ~2s attempt) when Redis is down, instead of arq's
        # default 5 retries: the caller turns this into a failed run + 503.
        self._settings = dataclasses.replace(RedisSettings.from_dsn(redis_url), conn_retries=0, conn_timeout=2)
        self._pool: ArqRedis | None = None

    async def enqueue_run(self, run_id: str) -> None:
        try:
            if self._pool is None:
                self._pool = await create_pool(self._settings, retry=0)
            # _job_id = run id: enqueueing the same run twice is a no-op.
            job = await self._pool.enqueue_job(EXECUTE_RUN_JOB, run_id, _job_id=f"run:{run_id}")
        except Exception as exc:  # noqa: BLE001 - any Redis/connection failure
            self._pool = None
            raise QueueUnavailableError(
                f"could not enqueue run on {self._settings.host}:{self._settings.port}: {exc}"
            ) from exc
        if job is None:
            raise QueueUnavailableError(f"run {run_id} is already queued")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


_queue: ArqRunQueue | None = None


def get_run_queue() -> RunQueue:
    global _queue
    if _queue is None:
        _queue = ArqRunQueue(get_settings().redis_url)
    return _queue
