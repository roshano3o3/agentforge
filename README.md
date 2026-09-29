# AgentForge

Production evaluation, safety testing, observability, and release gating for AI agents — a real, working system, not a metrics-dashboard demo.

**This is Phase 1 of a multi-phase build.** It is a small, complete vertical slice: publish a versioned dataset, run a real evaluation from the CLI against a real (synthetic) RAG app, persist every input/output/score, and inspect the same persisted run in a web dashboard. There is no queue, no release gate, no safety testing, no LLM-as-judge, and no authentication yet — see [Current limitations](#current-limitations) and [What's next](#whats-next).

## What actually exists right now

- A FastAPI backend (`apps/api`) with real SQLAlchemy 2 async models and an Alembic migration, backed by Postgres (via Docker Compose) or a local SQLite file.
- A CLI (`agentforge`) that publishes an immutable versioned dataset, runs a 5-case evaluation against an example RAG app, scores it with one deterministic evaluator, and persists everything.
- One evaluator: `heuristic_context_precision` — a deterministic document-ID overlap heuristic, explicitly **not** the RAGAS metric and **not** an LLM judge. See [`docs/evaluators.md`](docs/evaluators.md) for the exact formula.
- An example RAG app (`examples/rag_app`) over synthetic "Northwind Outfitters" policy documents, using a deterministic keyword-overlap retriever — no external API calls.
- A Next.js dashboard with one working page: **Runs** (list + detail), reading only real persisted data over HTTP. Regression / Trace Explorer / Safety are stubbed nav links, not fake pages.
- 19 automated tests (unit + integration + end-to-end CLI), all passing — see [Testing](#testing).

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

## Evaluator

See [`docs/evaluators.md`](docs/evaluators.md) for the exact formula, the zero-retrieval edge case definition, and why this is explicitly not RAGAS's `context_precision` or an LLM judge.

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for the component diagram, the evaluation-run lifecycle, the Phase 1 domain model, and the immutability guarantees (dataset versions, evaluation runs) and how they're enforced.

## Testing

```powershell
.\scripts\test.ps1
```

Runs `tests/unit` (the scoring formula — perfect/partial/zero-overlap/zero-retrieval, order-independence, no double-counting duplicates), `tests/integration` (dataset immutability, evaluation-run persistence and aggregation, against a real FastAPI app + real async SQLAlchemy session over in-memory SQLite), and `tests/e2e` (the actual `agentforge` CLI, as a subprocess, against a really-running uvicorn server — publish the real dataset file, run the real 5-case evaluation, then verify independently through the API that what the CLI printed is exactly what got persisted).

Last run in this environment: **19 passed, 0 failed** (Python 3.12.7, Windows, both Git Bash and native PowerShell).

No coverage percentage is claimed here because none has been measured.

## Repository layout

```
apps/api/          FastAPI backend, SQLAlchemy models, Alembic migration
apps/web/           Next.js dashboard (Runs page only, so far)
packages/core/       Shared Pydantic request/response schemas (api + cli)
packages/evaluators/  heuristic_context_precision (zero dependencies)
packages/sdk/         Adapter protocol + lightweight AgentForgeClient (httpx only)
cli/                  `agentforge` CLI (Typer)
examples/rag_app/     Synthetic RAG app: documents, deterministic retriever, adapter
datasets/             Versioned dataset YAML (rag_support_v1.yaml)
tests/                unit / integration / e2e
docs/                 architecture.md, evaluators.md
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
- **Docker/Postgres path is unverified in this environment.** Docker Desktop wasn't available when this phase was built; the Compose file and Dockerfile exist and were reviewed but not run. The SQLite path (Path A above) is what was actually executed and tested.
- **A known SQLite-only serialization quirk:** timestamps re-fetched from the SQLite dev DB can lose their explicit UTC-offset suffix (still the same instant) in a way Postgres's `DateTime(timezone=True)` column does not exhibit. Documented in `docs/architecture.md` and handled explicitly in the relevant test.

## What's next

Phase 2 (evaluation engine: more RAG evaluators, worker + queue, evidence at scale), Phase 3 (agent trajectory evaluation, LangGraph example agent), Phase 4 (regression engine, release policy, CI gate), Phase 5 (safety/adversarial testing), Phase 6 (OpenTelemetry tracing), Phase 7 (failure replay), remaining dashboard pages, then CI/CD and a reproducible benchmark. Not started; not claimed as done.

## License

[MIT](LICENSE)
