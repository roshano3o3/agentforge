"""arq worker entry point. Runs only inside the Linux `worker` container:

arq agentforge_worker.main.WorkerSettings
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from arq import func
from arq.connections import RedisSettings

from agentforge_api import tracing
from agentforge_api.config import get_settings
from agentforge_api.db.base import async_session_factory
from agentforge_api.queue import EXECUTE_REPLAY_JOB, EXECUTE_RUN_JOB
from agentforge_evaluators import ModelPrice, parse_pricing
from agentforge_worker.replay import execute_replay
from agentforge_worker.runner import execute_run

log = logging.getLogger("agentforge.worker")
# arq's CLI configures only its own "arq" logger; give ours a handler too so
# per-case progress shows up in `docker compose logs worker`.
if not log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s: %(message)s", "%H:%M:%S"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

# A whole run (all cases) must finish within this; per-case limits are the
# run's own timeout_seconds.
JOB_TIMEOUT_SECONDS = int(os.environ.get("AGENTFORGE_JOB_TIMEOUT_SECONDS", "3600"))


def load_pricing(path: str | None) -> dict[str, ModelPrice]:
    if not path:
        log.warning("AGENTFORGE_PRICING_FILE not set; estimated_cost will report 'no pricing entry'")
        return {}
    file = Path(path)
    if not file.exists():
        log.warning("pricing file %s not found; estimated_cost will report 'no pricing entry'", file)
        return {}
    return parse_pricing(yaml.safe_load(file.read_text(encoding="utf-8")))


async def startup(ctx: dict) -> None:
    ctx["pricing"] = load_pricing(os.environ.get("AGENTFORGE_PRICING_FILE"))
    collector = tracing.setup("agentforge-worker")
    log.info(
        "tracing: %s",
        "off (AGENTFORGE_TRACING)"
        if collector is None
        else "on (spans stored per case"
        + (", PII redaction on" if tracing.redaction_enabled() else ", PII redaction OFF")
        + (", OTLP export on)" if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") else ")"),
    )
    log.info("worker ready; priced models: %s", sorted(ctx["pricing"]) or "(none)")


async def run_job(ctx: dict, run_id: str, trace_context: dict[str, str] | None = None) -> str:
    return await execute_run(async_session_factory, run_id, pricing=ctx["pricing"], trace_context=trace_context)


async def replay_job(ctx: dict, replay_id: str, trace_context: dict[str, str] | None = None) -> str:
    return await execute_replay(async_session_factory, replay_id, pricing=ctx["pricing"], trace_context=trace_context)


class WorkerSettings:
    # max_tries=2: a hard-killed worker (OOM, SIGKILL) never runs the
    # cancellation handler, so arq's one retry restarts the run from scratch
    # (see runner._start). A cancelled job has already marked the run failed,
    # so its retry is a no-op.
    functions = [
        func(run_job, name=EXECUTE_RUN_JOB, timeout=JOB_TIMEOUT_SECONDS, max_tries=2),
        func(replay_job, name=EXECUTE_REPLAY_JOB, timeout=JOB_TIMEOUT_SECONDS, max_tries=2),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = startup
    max_jobs = int(os.environ.get("AGENTFORGE_WORKER_MAX_JOBS", "4"))
    keep_result = 3600
