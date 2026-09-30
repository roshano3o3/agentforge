# CLAUDE.md — AgentForge session brief

## What this is
AgentForge: evaluation, safety testing, observability, and release gating for AI
agents — a real working system, not a metrics-dashboard demo. Built in phases; each
phase is a small, complete, honestly documented vertical slice.

Read `README.md` (esp. "Current limitations") and `docs/architecture.md` before changing things.

## Stack
- API: FastAPI + SQLAlchemy 2 (async) + Alembic — `apps/api` (run state machine: `services/runs.py`)
- Worker: arq + Redis — `apps/worker` (`adapters.py` timeouts/capture, `runner.py` execution). Docker only.
- DB: Postgres via Docker Compose; SQLite via `aiosqlite` is a fallback that can't execute runs
- Web: Next.js dashboard — `apps/web` (Playwright e2e in `apps/web/e2e`)
- CLI: Typer (`agentforge`) — `cli/`; shared Pydantic schemas — `packages/core`
- Evaluators: `packages/evaluators` — `name@version` registry, 9 deterministic evaluators, aggregates
- SDK: `packages/sdk` (adapter contract: python `module:fn` + http); pricing: `config/pricing.yaml`
- Example app: `examples/rag_app` (synthetic "Northwind Outfitters"; also `fault_injection`, `http_server`)
- Python 3.12, Node 18+

## Rules (non-negotiable)
- **No invented metrics.** Every number shown in the CLI, dashboard, or docs comes from a real
  persisted run or a real test execution. No fabricated scores, coverage %, benchmarks, or vendor prices.
- **Label `local-deterministic` results as fixture-based.** Never present them as model quality,
  and never call any evaluator an LLM judgment (none is). Cost is always labeled "estimated".
- **Adapters are trusted local code**, run by the worker. Timeouts/capture contain bugs; not a sandbox.
- **Bind everything to localhost** (`127.0.0.1`): API, Postgres, Redis, dashboard default API URL.
  There is no auth, so nothing may be exposed publicly.
- **No fake auth.** Auth is either real or absent (and documented as absent).
- Don't claim something works unless it was actually run. Mark unverified paths as unverified.
- Immutability: published `DatasetVersion`s and completed/failed runs (with their results and
  metric scores) are frozen — service layer + DB triggers. Runs only target published versions.
  Only the worker writes results; there is no client route for it.

## Windows notes
- Use the PowerShell scripts in `scripts/`: `setup.ps1`, `docker-up.ps1`/`docker-down.ps1`,
  `seed-demo.ps1`, `dev-web.ps1`, `test.ps1 [-Postgres]`, `test-ui.ps1` (always Docker).
- **Workers run only inside Linux Docker containers**, never natively on Windows (tests call
  the worker's `execute_run` in-process, which is fine; the worker *service* is Docker only).
- Test isolation: Postgres DBs `agentforge_test` / `agentforge_e2e_test`, Redis DBs 1 / 2, and
  dedicated worker containers; dev uses `agentforge` + Redis DB 0. Never point tests at dev.
- Next.js dev server locks per project dir: stop `dev-web.ps1` before running `test-ui.ps1`.
- Lint locally before pushing: `ruff check .`, `ruff format --check .`, `mypy` (venv), and in
  `apps/web`: `npx eslint src e2e playwright.config.ts`, `npx tsc --noEmit`.
- PowerShell 5.1: native stderr + `$ErrorActionPreference="Stop"` aborts when output is redirected —
  relax to "Continue" around docker/npm and check `$LASTEXITCODE`. Don't edit text files with
  `Get-Content`/`Set-Content` (adds a BOM, can mangle UTF-8).

## Current status (2026-09-30)
- **Phase 1 done**, Docker/Postgres verified.
- **Phase 2 done**: worker + queue, python/http adapters, 9 evaluators, stored aggregates, run
  immutability triggers, CLI submit+poll, Runs list/detail UI with new-run form.
  Tests: SQLite 75 passed + 9 skipped; `test.ps1 -Postgres` 84 passed; `test-ui.ps1` 2 passed.
- **Pre-Phase-3 cleanup done**: per-case evaluator config (`default_evaluators` + case `evaluators`,
  frozen with the published version; see docs/evaluators.md). Example dataset pass rate 40% (2/5,
  fixture-based, 3 genuine failures). Tests: SQLite 101 passed + 11 skipped; Postgres 112; Playwright 2.
- Repo: https://github.com/roshano3o3/agentforge (public). CI jobs: lint (ruff check + format, mypy,
  eslint, tsc), python (Postgres + SQLite), browser (Playwright). Keep all of them green.
- Legacy Phase 2 fields `expected_answer_contains`/`_regex` are read-only; never rewrite published rows.

## Phase plan
1. Vertical slice: CLI eval, one deterministic evaluator, dashboard — **done**
2. Evaluation engine: evaluators, worker + queue (Docker), aggregates — **done**
3. Agent trajectory evaluation, LangGraph example agent
4. Regression engine, release policy, CI gate (`agentforge gate`)
5. Safety / adversarial testing
6. OpenTelemetry tracing, Trace Explorer
7. Failure replay
Then: remaining dashboard pages, reproducible benchmark.
