# CLAUDE.md — AgentForge session brief

## What this is
AgentForge: evaluation, safety testing, observability, and release gating for AI
agents — a real working system, not a metrics-dashboard demo. Built in phases; each
phase is a small, complete, honestly documented vertical slice.

Read `README.md` (esp. "Current limitations") and `docs/architecture.md` before changing things.

## Stack
- API: FastAPI + SQLAlchemy 2 (async) + Alembic — `apps/api`
- DB: Postgres via Docker Compose (intended); SQLite via `aiosqlite` (fallback, same code path),
  selected by `AGENTFORGE_DATABASE_URL`
- Web: Next.js dashboard — `apps/web` (Playwright e2e in `apps/web/e2e`)
- CLI: Typer (`agentforge`) — `cli/`; shared Pydantic schemas — `packages/core`
- Evaluators: `packages/evaluators` (only `heuristic_context_precision`); SDK: `packages/sdk`
- Example app: `examples/rag_app` (synthetic "Northwind Outfitters", deterministic retriever)
- Python 3.12, Node 18+

## Rules (non-negotiable)
- **No invented metrics.** Every number shown in the CLI, dashboard, or docs comes from a real
  persisted run or a real test execution. No fabricated scores, coverage %, or benchmarks.
- **Label `local-deterministic` results as fixture-based.** They come from a synthetic app and
  a deterministic heuristic, not a real model; never present them as model quality.
- **Adapters are trusted local code**, imported and run in-process. Not a sandbox; don't claim isolation.
- **Bind everything to localhost** (`127.0.0.1`): API, Postgres, dashboard default API URL.
  There is no auth, so nothing may be exposed publicly.
- **No fake auth.** Don't add placeholder logins, stub tokens, or "auth" that doesn't enforce anything.
  Auth is either real or absent (and documented as absent).
- Don't claim something works unless it was actually run. Mark unverified paths as unverified.
- Immutability: published `DatasetVersion`s are frozen (service layer + DB trigger). Change
  content only via `POST .../new-draft`. Runs may only target published versions.

## Windows notes
- Dev machine is Windows. Use the PowerShell scripts in `scripts/`, not a Makefile or `just`:
  `setup.ps1`, `db-migrate.ps1`, `dev-api.ps1`, `dev-web.ps1`, `seed-demo.ps1`,
  `test.ps1` (Python), `test-ui.ps1` (Playwright), `docker-up.ps1` / `docker-down.ps1`.
- **Workers (Phase 2+) run only inside Linux Docker containers**, never natively on Windows.
- Next.js dev server locks per project dir: stop `dev-web.ps1` before running `test-ui.ps1`.
- Open the dashboard via `127.0.0.1` or `localhost` (both allowed in `next.config.ts`).

## Current status
- **Phase 1 done** (HEAD `3bf2d55` before this file): Applications / Datasets / Runs pages,
  draft→published dataset versioning with DB-trigger immutability, CLI evaluation.
- Tests: 33 Python + 1 Playwright pass — **on SQLite only**.
- **Postgres/Docker path is unverified.** Docker is not installed on this machine
  (`docker --version` fails, as of 2026-09-30). Pending once Docker is available: `docker-up.ps1`,
  migrations, full Python + Playwright suites on Postgres, and confirming the PL/pgSQL trigger
  blocks a raw-SQL UPDATE on a published version's test cases.

## Phase plan
1. Vertical slice: CLI eval, one deterministic evaluator, dashboard — **done** (Postgres unverified)
2. Evaluation engine: more RAG evaluators, worker + queue (Docker), evidence at scale
3. Agent trajectory evaluation, LangGraph example agent
4. Regression engine, release policy, CI gate (`agentforge gate`)
5. Safety / adversarial testing
6. OpenTelemetry tracing, Trace Explorer
7. Failure replay
Then: remaining dashboard pages, CI/CD, reproducible benchmark.
