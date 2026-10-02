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
- Evaluators: `packages/evaluators` — `name@version` registry, 21 deterministic evaluators (9 answer/
  retrieval/measurement + 7 trajectory in `trajectory.py` + 5 safety in `safety.py`), aggregates; one dep: `jsonschema`
- Adversarial generator: `cli/agentforge_cli/adversarial.py` (pure; `agentforge adversarial generate`), profile
  `examples/invoice_agent/adversarial_profile.yaml`, output `datasets/invoice_agent_safety_v1.yaml` (35 variants)
- SDK: `packages/sdk` (adapter contract: python `module:fn` + http, optional `steps` trajectory);
  pricing: `config/pricing.yaml`
- Example apps: `examples/rag_app` (synthetic "Northwind Outfitters"; also `fault_injection`,
  `http_server`); `examples/invoice_agent` (LangGraph StateGraph + ToolNode, **scripted planner, not
  an LLM**; `answer_v1` and `answer_v2` with 5 deliberate regressions) + `datasets/invoice_agent_v1.yaml`
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
  Only the worker writes results; there is no client route for it. Agent steps (`agent_steps`) and a
  case's `trajectory` expectations are frozen the same way.
- Trajectories are only what the adapter reports. Never infer, fill in, or time steps on its behalf.
- A case's `safety` block (category, expectations) never reaches the adapter; only `scenario` does. Never put
  attack metadata in a scenario (a test asserts it).
- Never tune the example agent or the sample to a number: the seed (7) and v2's settings were fixed before running.

## Windows notes
- Use the PowerShell scripts in `scripts/`: `setup.ps1`, `docker-up.ps1`/`docker-down.ps1`,
  `seed-demo.ps1`, `dev-web.ps1`, `test.ps1 [-Postgres]`, `test-ui.ps1` (always Docker).
- **Workers run only inside Linux Docker containers**, never natively on Windows (tests call
  the worker's `execute_run` in-process, which is fine; the worker *service* is Docker only).
- Test isolation: Postgres DBs `agentforge_test` / `agentforge_e2e_test`, Redis DBs 1 / 2, and
  dedicated worker containers; dev uses `agentforge` + Redis DB 0. Never point tests at dev.
- Next.js dev server locks per project dir: stop `dev-web.ps1` before running `test-ui.ps1`.
- Lint locally before pushing: `ruff check .`, `ruff format --check .`, `mypy` (venv), and in
  `apps/web`: `npx eslint src e2e playwright.config.ts`, `npm run typecheck` (next typegen + tsc).
- PowerShell 5.1: native stderr + `$ErrorActionPreference="Stop"` aborts when output is redirected —
  relax to "Continue" around docker/npm and check `$LASTEXITCODE`. Don't edit text files with
  `Get-Content`/`Set-Content` (adds a BOM, can mangle UTF-8).

## Current status (2026-10-02, Phase 6A in review)
- **Phase 1 done**, Docker/Postgres verified.
- **Phase 2 done**: worker + queue, python/http adapters, 9 evaluators, stored aggregates, run
  immutability triggers, CLI submit+poll, Runs list/detail UI with new-run form.
  Tests: SQLite 75 passed + 9 skipped; `test.ps1 -Postgres` 84 passed; `test-ui.ps1` 2 passed.
- **Pre-Phase-3 cleanup done**: per-case evaluator config (`default_evaluators` + case `evaluators`,
  frozen with the published version; see docs/evaluators.md). Example dataset pass rate 40% (2/5,
  fixture-based, 3 genuine failures). Tests: SQLite 101 passed + 11 skipped; Postgres 112; Playwright 2.
- Repo: https://github.com/roshano3o3/agentforge (public). CI jobs: lint (ruff check + format, mypy,
  eslint, tsc), python (Postgres + SQLite), browser (Playwright). Keep all of them green.
- **Phase 3 done** (agent trajectory evaluation): `agent_steps` table + `test_cases.trajectory`
  (migration `e8b4c2d61a9f`, triggers on Postgres and SQLite); worker validates and saves steps; 7
  trajectory evaluators; LangGraph invoice agent v1/v2; run detail trajectory timeline (failing steps
  red with the evaluator's reason, "show more" for long args/results). Real runs on the Docker stack:
  v1 10/10 cases passed (100%), v2 4/10 (40%) -- fixture-based (scripted planner). README has the table.
  Tests: SQLite 150 passed + 14 skipped; Postgres 164; Playwright 4.
- Legacy Phase 2 fields `expected_answer_contains`/`_regex` are read-only; never rewrite published rows.
- The dashboard's draft editor PATCHes whole cases: any new TestCase field must be carried through
  `toTestCaseInput` (apps/web/src/app/datasets/[name]/page.tsx) or a UI edit silently erases it.
- Playwright case screenshots: grow the viewport to the page height before measuring/clipping
  (`screenshotCase` in e2e/trajectory-flow.spec.ts); fullPage/element captures came out a line off.

- **Phase 4 part A done** (backend + CLI): baselines (pointer per app+env), regression (`GET /regression`,
  400 on different dataset versions), release policy in `agentforge.yaml` (`agentforge_evaluators/release.py`,
  validated on load), `agentforge gate` (API computes checks; immutable `release_decisions`, migration
  `f3c7a9e2b510`; exit 0/1/2; `$GITHUB_STEP_SUMMARY`). Real gate: v1->v1 PASSED, v1->v2 FAILED.
  Tests: SQLite 190 passed + 17 skipped; Postgres 207; Playwright 4 (unchanged).
- **Phase 4 part B done** (2026-10-01): gate workflow `.github/workflows/agentforge-gate.yml` + `scripts/ci-gate.sh`
  (base branch's agent = production baseline, PR's agent = candidate, base branch's policy; one PR comment;
  check fails on FAILED); Regression + Baselines pages. Demo PRs left open, never merge:
  #2 `demo/v1-agent` (`BEHAVIOR = V1`) PASSED 8/8; #3 `demo/v2-agent` (`BEHAVIOR = V2`) FAILED 3/8, pass rate
  1 -> 0.4, 6 newly failing (3 critical). README "Release gate in CI" + `docs/screenshots/ci-gate-pr3-*.png`.
  PR #1 (`demo/invoice-agent-fast-paths` -> `phase4`) is stale: comment-only diff, gate PASSED.
  Tests: Playwright 6 (local + CI).
- Playwright sets FORCE_COLOR on GitHub Actions; Rich then emits ANSI even through a pipe. e2e specs that
  regex CLI output must drop FORCE_COLOR / set NO_COLOR (see regression-flow.spec.ts).
- **Phase 5 part A done on branch `phase5-safety`, awaiting review** (2026-10-01): `test_cases.scenario` + `.safety`
  (migration `a5d81c3f9e27`), dataset `content_hash` (computed, published only; `agentforge_core.hashing`), worker
  passes `scenario` (python kwarg / http field; adapter without it -> error result), 5 safety evaluators, per-category
  aggregates (`aggregates.safety.by_category`), generator, invoice agent defenses D1-D5 (v2 lacks D1-D3). Docker runs:
  v1 35/35; v2 15/35 (injection_direct 2/5, indirect 0/5, poisoned 1/5, malformed 5/5, tool_failure 2/5, pii 5/5,
  unauthorized 0/5). Offline over all 226 eligible variants v1 still fails 15 (keyword-intent hijack, `$0` refund).
  Tests: SQLite 236 + 17 skipped; Postgres 253; Playwright 6. Regenerating the committed dataset must stay byte-identical.
- **Phase 5 part B done, merged to main** (2026-10-01): release metrics `safety.<category>.pass_rate` + pooled
  `safety.injection.pass_rate`; `safety_policy` in agentforge.yaml (minimums 0.8, injection drop <= 0.02,
  unauthorized_tool/pii_probe max_drop 0); `agentforge gate --policy/--title`; ci-gate.sh gates both datasets,
  one combined comment (safety skipped if the base branch lacks it). Stress dataset (226, not gated): v1 211/226,
  v2 81/226. `/safety` dashboard page. Demo PR #4 (D2 off only, never merge): trajectory PASSED 8/8, safety
  FAILED 9/11 (injection_indirect 0.2 < 0.8; injection drop 0.4). Tests: SQLite 248 + 17 skipped; Postgres 265;
  Playwright 8. Branch `phase5-safety` == main at the merge.
- **Pre-Phase-6 (on main, 2026-10-02)**: baselines keyed by (application, environment, dataset), migration
  `b9e4f1c27d36`; CI sets `production` per dataset. Gate workflow now checks out the base branch by ref
  (`pull_request.base.sha` is stale: PRs #2/#3 gated against b9f5a10 and skipped safety until fixed in f869f87).
  Re-run: PR #2 PASSED (trajectory 8/8, safety 11/11), PR #3 FAILED (trajectory 3/8, safety 4/11).
- **Phase 6 part A on `phase6-otel`**: `agentforge_api/tracing.py` (provider + SpanCollector, OTLP only if
  OTEL_EXPORTER_OTLP_ENDPOINT, AGENTFORGE_TRACING=off), `trace_spans` + `agent_steps.span_id` (migration
  `c3d7e2a94b18`, immutability triggers), job carries the API span's traceparent, HTTP adapter sends it,
  `agentforge_sdk.tracing` (optional OTel) used by the invoice agent (planner_decision / execute_tool spans,
  `agentforge.planner.source`) and rag_app (retrieval). `GET /traces/{result_id}`. Jaeger: compose profile
  `tracing`. Overhead (Docker, v1, export off, medians excl. cold start): 10 cases 215 -> 265 ms, 35 cases
  744 -> 1058 ms (+5 / +9 ms per case, mostly span-row inserts). Tests: SQLite 259 + 19 skipped; Postgres 278;
  Playwright 8.
- The API image must install packages/sdk (agentforge_api.tracing imports agentforge_sdk.tracing); the venv
  has everything, so only a Docker run catches a missing package in an image.
- Never invent spans from steps; agent spans come only from the agent. Span attributes store content (tool
  results incl. PII), cut at 16k chars.
- The new-run form shows one checkbox per registered evaluator: adding one changes run-flow.spec.ts's count.
- Writing big Python patches through a bash heredoc breaks on quotes/`\n`: write the script to the scratchpad instead.
- Rich parses `[...]` in printed strings as markup and drops it: `escape()` any interpolated data
  (tags, metric names like `newly_failing[tag=critical]`, error messages).

## Phase plan
1. Vertical slice: CLI eval, one deterministic evaluator, dashboard — **done**
2. Evaluation engine: evaluators, worker + queue (Docker), aggregates — **done**
3. Agent trajectory evaluation, LangGraph example agent — **done**
4. Regression engine, release policy, CI gate (`agentforge gate`) — **done** (A: backend + CLI;
   B: gate workflow, Regression/Baselines pages, demo PRs #2 (v1, PASSED) and #3 (v2, FAILED) left open)
5. Safety / adversarial testing — **done** (A: generator, safety evaluators, per-category results;
   B: safety_policy in the CI gate, stress dataset, Safety dashboard page, demo PR #4)
6. OpenTelemetry tracing, Trace Explorer — part A (instrumentation, propagation, storage, export, API) **done** on
   branch `phase6-otel`, in review; part B: Trace Explorer page
7. Failure replay
Then: remaining dashboard pages, reproducible benchmark.
8. Polish & proof — README rewrite (tagline, 30-sec demo GIF, architecture diagram, Why AgentForge,
   metrics, trajectory eval, adversarial testing, CI/CD, failure replay, benchmarks, quick start, API,
   screenshots, tests), K8s manifests, MCP adapter, a real model-comparison run (OpenAI / Claude /
   optional Llama via vLLM; needs API keys, results in `benchmarks/` JSON with date + dataset hash),
   and resume bullets generated only from measured results in the repo.
