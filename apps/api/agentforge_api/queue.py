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
EXECUTE_REPLAY_JOB = "execute_replay"


class QueueUnavailableError(RuntimeError):
    pass


class RunQueue(Protocol):
    # trace_context: the W3C carrier ({"traceparent": ...}) of the API's
    # run-creation span, so the worker's spans continue that trace.
    async def enqueue_run(self, run_id: str, trace_context: dict[str, str] | None = None) -> None: ...

    # Same, for one replay (Phase 7): the worker re-runs one case.
    async def enqueue_replay(self, replay_id: str, trace_context: dict[str, str] | None = None) -> None: ...


class ArqRunQueue:
    def __init__(self, redis_url: str) -> None:
        # Fail fast (one ~2s attempt) when Redis is down, instead of arq's
        # default 5 retries: the caller turns this into a failed run + 503.
        self._settings = dataclasses.replace(RedisSettings.from_dsn(redis_url), conn_retries=0, conn_timeout=2)
        self._pool: ArqRedis | None = None

    async def enqueue_run(self, run_id: str, trace_context: dict[str, str] | None = None) -> None:
        await self._enqueue(EXECUTE_RUN_JOB, "run", run_id, trace_context)

    async def enqueue_replay(self, replay_id: str, trace_context: dict[str, str] | None = None) -> None:
        await self._enqueue(EXECUTE_REPLAY_JOB, "replay", replay_id, trace_context)

    async def _enqueue(self, job_name: str, what: str, item_id: str, trace_context: dict[str, str] | None) -> None:
        try:
            if self._pool is None:
                self._pool = await create_pool(self._settings, retry=0)
            # _job_id = the item's id: enqueueing the same run (or replay) twice is a no-op.
            job = await self._pool.enqueue_job(job_name, item_id, trace_context or None, _job_id=f"{what}:{item_id}")
        except Exception as exc:  # noqa: BLE001 - any Redis/connection failure
            self._pool = None
            raise QueueUnavailableError(
                f"could not enqueue {what} on {self._settings.host}:{self._settings.port}: {exc}"
            ) from exc
        if job is None:
            raise QueueUnavailableError(f"{what} {item_id} is already queued")

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
