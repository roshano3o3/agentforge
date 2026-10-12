# AgentForge API. Runs in Linux containers only. It persists and serves data
# and enqueues runs; it never executes adapters (that's the worker's job).
FROM python:3.12-slim

WORKDIR /app

COPY packages/core /app/packages/core
COPY packages/evaluators /app/packages/evaluators
# The SDK, for agentforge_sdk.tracing's span-attribute encoding (shared with
# agents, so a span's attributes look the same whoever recorded it).
COPY packages/sdk /app/packages/sdk
COPY apps/api /app/apps/api

RUN pip install --no-cache-dir -e /app/packages/core -e /app/packages/evaluators -e /app/packages/sdk -e /app/apps/api

WORKDIR /app/apps/api

EXPOSE 8000

CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn agentforge_api.main:app --host 0.0.0.0 --port 8000"]
