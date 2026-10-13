# The `agentforge` CLI with the repo's datasets and agentforge.yaml: docker-compose.yml's one-shot `seed`
# service runs `agentforge demo seed` with it against the API. Build context: the repo root.
FROM python:3.12-slim

WORKDIR /app

COPY packages/core /app/packages/core
COPY packages/evaluators /app/packages/evaluators
COPY packages/sdk /app/packages/sdk
COPY cli /app/cli
RUN pip install --no-cache-dir -e /app/packages/core -e /app/packages/evaluators -e /app/packages/sdk -e /app/cli

COPY datasets /app/datasets
COPY agentforge.yaml /app/agentforge.yaml

CMD ["agentforge", "demo", "seed", "--root", "/app", "--api-url", "http://api:8000"]
