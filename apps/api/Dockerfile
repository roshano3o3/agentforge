# AgentForge API. Runs in Linux containers only — see README for why the
# CLI/adapters run natively on Windows while the API+DB stay in Docker.
FROM python:3.12-slim

WORKDIR /app

# Only the local packages the API actually needs at runtime (agentforge-core
# for shared schemas). Evaluators/SDK/CLI run client-side, not in this image.
COPY packages/core /app/packages/core
COPY apps/api /app/apps/api

RUN pip install --no-cache-dir -e /app/packages/core -e /app/apps/api

WORKDIR /app/apps/api

EXPOSE 8000

CMD ["sh", "-c", "python -m alembic upgrade head && python -m uvicorn agentforge_api.main:app --host 0.0.0.0 --port 8000"]
