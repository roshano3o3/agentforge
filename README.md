# AgentForge

Production evaluation, safety testing, observability, and release gating for AI agents — a real, working system, not a metrics-dashboard demo.

**This is Phase 1 of a multi-phase build.** It is a small, complete vertical slice: publish a versioned dataset, run a real evaluation from the CLI against a real (synthetic) RAG app, persist every input/output/score, and inspect the same persisted run in a web dashboard. There is no queue, no release gate, no safety testing, no LLM-as-judge, and no authentication yet — see [Current limitations](#current-limitations) and [What's next](#whats-next).

## What actually exists right now

- A FastAPI backend (`apps/api`) with real SQLAlchemy 2 async models and an Alembic migration, backed by Postgres (via Docker Compose) or a local SQLite file.
- A CLI (`agentforge`) that publishes an immutable versioned dataset, runs a 5-case evaluation against an example RAG app, scores it with one deterministic evaluator, and persists everything.
- One evaluator: `heuristic_context_precision` — a deterministic document-ID overlap heuristic, explicitly **not** the RAGAS metric and **not** an LLM judge. See [`docs/evaluators.md`](docs/evaluators.md) for the exact formula.
- An example RAG app (`examples/rag_app`) over synthetic "Northwind Outfitters" policy documents, using a deterministic keyword-overlap retriever — no external API calls.
- A Next.js dashboard with three working pages — **Applications**, **Datasets**, **Runs** (each list + detail) — reading and writing only real persisted data over HTTP. Regression / Trace Explorer / Safety are stubbed nav links, not fake pages.
- The Datasets page lets you create a dataset, compose test cases, and publish an immutable version through the UI — and lets you try to edit a published version and watch the API reject it with a real `409`, evidence in `docs/screenshots/`.
- 27 automated tests: 26 Python (unit + API integration + CLI-subprocess end-to-end) and 1 real browser test (Playwright/Chromium) that drives the actual UI — see [Testing](#testing).

Everything in the CLI table output and the dashboard comes from a real, persisted `EvaluationRun`/`EvaluationResult` row. Nothing is hardcoded.

## Quick start (Windows)

Two setup paths. **Path A (no Docker, SQLite)** is the one actually run and verified end-to-end while building this phase. **Path B (Docker + Postgres)** is the intended production-shaped setup; the Compose file and Dockerfile are provided and were reviewed, but Docker Desktop was not available in the environment this phase was built in, so it has not been run here — try it and file an issue if something's off.

### Path A — no Docker, SQLite (verified)

Prerequisites: Python 3.12+, Node.js 18+, PowerShell.

```powershell
git clone <this-repo-url> agentforge
cd agentforge

.\scripts\setup.ps1        # venv + all local packages (editable) + npm install
.\scripts\db-migrate.ps1   # creates apps\api\agentforge_dev.db via Alembic
```

In one terminal:

```powershell
.\scripts\dev-api.ps1      # FastAPI on http://127.0.0.1:8000 (localhost only)
```

In a second terminal:

```powershell
.\scripts\dev-web.ps1      # Next.js dashboard on http://127.0.0.1:3000
```

In a third terminal, publish the dataset and run the evaluation:

```powershell
.\scripts\seed-demo.ps1
```

Then open <http://127.0.0.1:3000/runs>.

### Path B — Docker + Postgres (not run in this environment)

Prerequisites: Docker Desktop (with WSL2), Node.js 18+.

```powershell
.\scripts\docker-up.ps1    # builds + starts postgres and api, runs migrations automatically
.\scripts\seed-demo.ps1    # same CLI commands, now pointed at the Postgres-backed API
.\scripts\dev-web.ps1      # web still runs natively, not containerized, for a fast dev loop
```

`.\scripts\docker-down.ps1` stops it (`-Volumes` also wipes the Postgres data volume).

## Example evaluation (real output)

This is actual output from `.\scripts\seed-demo.ps1` against the dataset in `datasets/rag_support_v1.yaml` and the example app in `examples/rag_app`:

```
Evaluating rag-assistant@v1 against rag-support v1 (5 case(s)) using provider local-deterministic...
       Run 92f993da-efaf-47d1-a127-a1a8480b19f6 - rag-assistant@v1
┌──────────────────────────────┬───────┬────────┬──────────────┬────────┐
│ Case                         │ Score │ Passed │ Latency (ms) │ Status │
├──────────────────────────────┼───────┼────────┼──────────────┼────────┤
│ loyalty-points-return-001    │ 0.50  │ FAIL   │ 0            │ ok     │
│ out-of-scope-sponsorship-001 │ 0.00  │ FAIL   │ 0            │ ok     │
│ refund-policy-001            │ 0.50  │ FAIL   │ 0            │ ok     │
│ shipping-time-001            │ 0.50  │ FAIL   │ 0            │ ok     │
│ warranty-outerwear-001       │ 1.00  │ PASS   │ 0            │ ok     │
└──────────────────────────────┴───────┴────────┴──────────────┴────────┘

5 case(s) - mean heuristic_context_precision=0.500, pass_rate=20% (threshold=0.7), avg_latency=0.0ms
evaluator=heuristic_context_precision@1.0.0  provider=local-deterministic  dataset=rag-support v1  commit=(none)
```

These scores are not curated to look good — a 20% pass rate against a 0.7 threshold is the real, deterministic result of this retriever against these 5 cases, and it's a useful demonstration that the heuristic is measuring something real (see `docs/evaluators.md` for why `out-of-scope-sponsorship-001` legitimately scores 0.0, and why partial-overlap cases score 0.5). `commit=(none)` because this evaluation was run before the repository's first commit existed; once you run it inside a git checkout with commits, the real `HEAD` SHA is recorded.

Inspect the same run at `http://127.0.0.1:3000/runs/<run-id>` — every case's input, expected relevant doc IDs, retrieved doc IDs, score, and evidence JSON is there, sourced from the same `GET /runs/{id}` the CLI could also call (`agentforge runs show <run-id>`).

## Applications, Datasets, and the immutability demo

`http://127.0.0.1:3000/applications` and `.../datasets` are real CRUD-ish pages, not the CLI-only flow from earlier in this phase:

- **Applications**: create one, view its versions. (There's no separate `Project` entity — Application is the top-level container; see `docs/architecture.md`.)
- **Datasets**: create one, compose test cases in a draft (client-side, unpublished), publish — which always creates a new, immutable `DatasetVersion`. Each published test case has a **"Try to edit"** button that sends a real `PATCH` to the API and shows the response inline. The API always rejects it with `409 Conflict` (`PATCH /datasets/{name}/versions/{version}` is a dedicated route that exists purely to give that explicit rejection, rather than a route not existing at all).

Real screenshots from an actual Playwright run, in `docs/screenshots/`: [`applications.png`](docs/screenshots/applications.png), [`dataset-detail.png`](docs/screenshots/dataset-detail.png) (a published version with its test cases), [`dataset-edit-rejected.png`](docs/screenshots/dataset-edit-rejected.png) (the live `HTTP 409` response rendered in the UI), [`runs.png`](docs/screenshots/runs.png), [`run-detail.png`](docs/screenshots/run-detail.png).

### A real bug this Playwright test caught: the Runs page likely never actually worked via `127.0.0.1`

Building the Playwright test surfaced a genuine bug in the previous state of this repo, not just in the new pages. Next.js 16 blocks cross-origin requests to dev-only resources (JS chunks, HMR) by default; accessing the dev server via `127.0.0.1` instead of `localhost` counts as cross-origin and gets silently blocked — the page's HTML shell renders, but the client JavaScript bundle never loads, so no `"use client"` component's `useEffect` ever runs, so **no client-side `fetch` to the API ever happens.** The page just sits on its loading state forever, with no console error, no failed network request — nothing in the browser to indicate why.

The original README told you to open `http://127.0.0.1:3000/runs`. Before the fix below, that URL would never have actually loaded any data — only `http://localhost:3000/runs` would have worked. This was not caught during Phase 1 because the "Runs page verification" done at the time was CORS-header-and-build-only, explicitly flagged as unable to load a real browser. Fixed in `apps/web/next.config.ts` (`allowedDevOrigins: ["127.0.0.1", "localhost"]`) and confirmed working via both the Playwright suite and a standalone headless-browser check against the original `:3000`/`:8000` setup.

## Evaluator

See [`docs/evaluators.md`](docs/evaluators.md) for the exact formula, the zero-retrieval edge case definition, and why this is explicitly not RAGAS's `context_precision` or an LLM judge.

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the component diagram, the evaluation-run lifecycle, the Phase 1 domain model, and the immutability guarantees (dataset versions, evaluation runs) and how they're enforced.

## Testing

```powershell
.\scripts\test.ps1      # Python: unit + integration + CLI end-to-end (no browser)
.\scripts\test-ui.ps1   # Playwright: real Chromium, drives the actual UI
```

`test.ps1` runs `tests/unit` (the scoring formula — perfect/partial/zero-overlap/zero-retrieval, order-independence, no double-counting duplicates), `tests/integration` (dataset immutability including the `409` edit-rejection, application/dataset listing, evaluation-run persistence and aggregation — against a real FastAPI app + real async SQLAlchemy session over in-memory SQLite), and `tests/e2e` (the actual `agentforge` CLI, as a subprocess, against a really-running uvicorn server). **None of this touches a browser or the frontend.**

`test-ui.ps1` runs `apps/web/e2e/dataset-flow.spec.ts` in real Chromium (via Playwright — no Chrome extension needed, it drives its own browser). It spins up its own API (fresh temp SQLite, port 8010) and web server (port 3010), seeds one real run via the CLI, then: loads Applications, creates a dataset through the UI, composes and publishes two test cases, opens the published version, clicks "Try to edit," confirms the API's live `409` is what's rendered, independently re-fetches the version over the API to prove the content is actually untouched, then loads Runs and a run detail page. Screenshots go to `docs/screenshots/`.

**Note:** Next.js's dev server holds a lock per project directory, not per port — `test-ui.ps1` will fail to start if `dev-web.ps1` (or any other `next dev` in this repo) is already running. Stop it first.

Last run in this environment: **26 Python tests passed** (Python 3.12.7, Windows, both Git Bash and native PowerShell) and **1/1 Playwright test passed** (Chromium).

No coverage percentage is claimed here because none has been measured.

## Repository layout

```
apps/api/          FastAPI backend, SQLAlchemy models, Alembic migration
apps/web/           Next.js dashboard: Applications, Datasets, Runs pages + e2e/ (Playwright)
packages/core/       Shared Pydantic request/response schemas (api + cli)
packages/evaluators/  heuristic_context_precision (zero dependencies)
packages/sdk/         Adapter protocol + lightweight AgentForgeClient (httpx only)
cli/                  `agentforge` CLI (Typer)
examples/rag_app/     Synthetic RAG app: documents, deterministic retriever, adapter
datasets/             Versioned dataset YAML (rag_support_v1.yaml)
tests/                unit / integration / e2e (Python — no browser)
docs/                 architecture.md, evaluators.md, screenshots/
scripts/              PowerShell setup/dev/test scripts (no Makefile, no `just` dependency)
infrastructure/       reserved for later phases (Kubernetes, etc.) — empty in Phase 1
```

## Current limitations

Read this before assuming a feature exists.

- **No queue, no worker.** The CLI executes the adapter and evaluator synchronously, in-process. A future worker is required to run inside a Linux Docker container, never natively on Windows — this is a standing project constraint, not just a Phase 1 note.
- **No release gate.** `agentforge gate`, regression comparison, and `ReleasePolicy` don't exist yet. `agentforge.yaml` (from `agentforge init`) is scaffolded but not read by anything yet.
- **No safety/adversarial testing.** No `SafetyFinding`, no attack categories, no `Safety` dashboard page (it's a disabled nav link).
- **No trace/span persistence.** No OpenTelemetry instrumentation, no Trace Explorer (disabled nav link).
- **No agent trajectory evaluation.** The example app is a RAG app, not a tool-using agent; that's Phase 3.
- **One evaluator, one metric.** `heuristic_context_precision` only. No RAGAS faithfulness/answer-relevance/groundedness, no LLM-as-judge, no citation correctness.
- **No authentication.** The API has no auth of any kind. Everything is bound to `127.0.0.1` (localhost) — Postgres, the API, and the dashboard's default API URL — precisely because there is no access control. **This setup is not ready for any kind of public or shared deployment.**
- **Trusted, unsandboxed adapter execution.** The CLI imports and calls the adapter function directly, in-process. This is fine for example/local code you wrote yourself; it is not a sandbox and provides no isolation from arbitrary code.
- **The example RAG app is entirely synthetic.** "Northwind Outfitters" is a fictional retailer invented for this repo; its policy documents are made up. The keyword-overlap retriever is deliberately simple (no embeddings, no ML) so the whole system runs offline with zero API keys.
- **Docker/Postgres path is unverified in this environment.** Docker Desktop is not installed on the machine this phase was built on (`docker --version` fails in both Git Bash and PowerShell) — the Compose file and Dockerfile exist and were reviewed but never actually run. The SQLite path (Path A above) is what was actually executed and tested.
- **A known SQLite-only serialization quirk:** timestamps re-fetched from the SQLite dev DB can lose their explicit UTC-offset suffix (still the same instant) in a way Postgres's `DateTime(timezone=True)` column does not exhibit. Documented in `docs/architecture.md` and handled explicitly in the relevant test.
- **"Editing" a test case only exists pre-publish.** Once a `DatasetVersion` is published it is genuinely immutable — there is no way to change a test case in place, in the UI or the API, by design. The Datasets page's draft composer (add/remove rows client-side, then publish) is the only place anything resembling "editing" happens; the published-version "Try to edit" button exists specifically to demonstrate the `409` rejection, not to provide a real edit path.
- **`agentforge.yaml` (from `agentforge init`) is still not read by anything.** No config-driven behavior yet — flags to the CLI are still the only way to configure a run.

## What's next

Phase 2 (evaluation engine: more RAG evaluators, worker + queue, evidence at scale), Phase 3 (agent trajectory evaluation, LangGraph example agent), Phase 4 (regression engine, release policy, CI gate), Phase 5 (safety/adversarial testing), Phase 6 (OpenTelemetry tracing), Phase 7 (failure replay), remaining dashboard pages, then CI/CD and a reproducible benchmark. Not started; not claimed as done.

## License

[MIT](LICENSE)
