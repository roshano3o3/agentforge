from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentforge_api import tracing
from agentforge_api.config import get_settings
from agentforge_api.routers import applications, datasets, evaluators, release, replays, runs, traces

settings = get_settings()
tracing.setup("agentforge-api")

app = FastAPI(
    title="AgentForge API",
    version="0.2.0",
    description=(
        "Applications, immutable datasets, and evaluation runs. Runs are created here and "
        "executed by the arq worker (Docker only); the API never executes adapters itself."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(applications.router)
app.include_router(datasets.router)
app.include_router(evaluators.router)
app.include_router(runs.router)
app.include_router(release.router)
app.include_router(traces.router)
app.include_router(replays.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
