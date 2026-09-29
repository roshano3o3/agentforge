from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentforge_api.config import get_settings
from agentforge_api.routers import applications, datasets, runs

settings = get_settings()

app = FastAPI(
    title="AgentForge API",
    version="0.1.0",
    description="Phase 1 vertical slice: applications, immutable datasets, evaluation runs.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(applications.router)
app.include_router(datasets.router)
app.include_router(runs.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
