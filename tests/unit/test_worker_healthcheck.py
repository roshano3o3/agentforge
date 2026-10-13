"""The worker's probe (`python -m agentforge_worker.healthcheck`): unhealthy, with reasons, when its dependencies
can't be reached. The healthy path is exercised by the kind deployment (scripts/k8s-kind.sh), where both probes
gate the worker pods' readiness."""

from __future__ import annotations

import asyncio

import pytest

from agentforge_api.config import get_settings
from agentforge_worker import healthcheck


def test_unreachable_redis_and_database_are_both_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens on port 1
    monkeypatch.setenv("AGENTFORGE_DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/db")
    get_settings.cache_clear()
    try:
        problems = asyncio.run(healthcheck.check())
    finally:
        get_settings.cache_clear()
    assert [p.split(":")[0] for p in problems] == ["redis", "database"]
    with pytest.raises(SystemExit) as exit_info:
        monkeypatch.setattr(healthcheck, "check", lambda: _done(problems))
        healthcheck.main()
    assert exit_info.value.code == 1


async def _done(problems: list[str]) -> list[str]:
    return problems
