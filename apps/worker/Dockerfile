# AgentForge worker: executes evaluation runs. Linux container only -- the
# worker never runs natively on Windows (see README).
FROM python:3.12-slim

WORKDIR /app

# Everything the worker imports: shared schemas, evaluators, the SDK's
# adapter contract, the API's models/services, and the example adapters it
# can run. Python adapters must be installed in this image to be importable.
COPY packages/core /app/packages/core
COPY packages/evaluators /app/packages/evaluators
COPY packages/sdk /app/packages/sdk
COPY apps/api /app/apps/api
COPY apps/worker /app/apps/worker
COPY examples/rag_app /app/examples/rag_app
# The invoice agent's source is overridable so the CI release gate can build a
# baseline image with the base branch's agent and everything else from the PR.
ARG INVOICE_AGENT_DIR=examples/invoice_agent
COPY ${INVOICE_AGENT_DIR} /app/examples/invoice_agent
COPY config /app/config

RUN pip install --no-cache-dir \
    -e /app/packages/core \
    -e /app/packages/evaluators \
    -e /app/packages/sdk \
    -e /app/apps/api \
    -e /app/apps/worker \
    -e /app/examples/rag_app \
    -e /app/examples/invoice_agent

ENV AGENTFORGE_PRICING_FILE=/app/config/pricing.yaml
WORKDIR /app/apps/worker

# Recorded on every run and replay as its code version (agentforge_worker.provenance).
# Last, so a new commit doesn't invalidate the cached layers above.
ARG AGENTFORGE_GIT_SHA=
ENV AGENTFORGE_GIT_SHA=${AGENTFORGE_GIT_SHA}

CMD ["arq", "agentforge_worker.main.WorkerSettings"]
