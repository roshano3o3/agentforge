# AgentForge

Production evaluation, safety testing, observability, and release gating for AI agents — a real, working system, not a metrics-dashboard demo.

**This is Phase 2 of a multi-phase build: the evaluation engine.** Runs are submitted through the API (from the CLI or the dashboard), queued in Redis, and executed by an [arq](https://arq-docs.helpmanual.io/) worker that runs **only inside a Linux Docker container**. The worker calls the application's adapter for every test case of a *published* dataset version (with a per-case timeout and full exception capture), scores each case with a registry of versioned deterministic evaluators, and stores results, reasons, evidence and run-level aggregates. Finished runs are immutable, enforced in the service layer and by Postgres triggers. There is still no release gate, no safety testing, no tracing, no LLM-as-judge, and no authentication — see [Current limitations](#current-limitations).

## What actually exists right now

- **API** (`apps/api`, FastAPI + async SQLAlchemy + Alembic, Postgres in Docker or SQLite as a fallback): applications, draft→published dataset versions, and runs. `POST /runs` creates a `pending` run and enqueues it; there is deliberately **no endpoint for a client to submit results or scores**.
- **Worker** (`apps/worker`, arq + Redis, Docker only): `pending → running → completed | failed`. Two adapter kinds, both **trusted local code** (not a sandbox):
  - `python` — `"module:function"`, imported and called in-process by the worker (must be installed in the worker image);
  - `http` — a URL the worker POSTs `{"input", "case_key"}` to (contract in [`packages/sdk/agentforge_sdk/adapter.py`](packages/sdk/agentforge_sdk/adapter.py)).
  A case that raises (even `SystemExit`), hangs past the timeout, or returns a malformed output is recorded as an `error`/`timeout` result with its exception type, message and traceback; the run and the worker carry on.
- **Evaluators** (`packages/evaluators`, zero dependencies, all deterministic — none is an LLM judge), each registered as `name@version` and pinned per run: `exact_match`, `answer_contains`, `answer_regex`, `heuristic_context_precision`, `heuristic_context_recall`, `citation_correctness`, `latency`, `token_usage`, `estimated_cost`. Every result stores score and/or measured value, pass/fail, a human-readable reason, evidence, and the evaluator version. Exact definitions: [`docs/evaluators.md`](docs/evaluators.md).
- **Aggregates**, stored on the run when it finishes: pass rate, per-metric means and pass rates, P50/P95 latency (nearest-rank, ok cases only), and total **estimated** cost (adapter-reported tokens × rates from [`config/pricing.yaml`](config/pricing.yaml), which ships with no vendor prices).
- **Labels:** every result of a `local-deterministic` run is labeled **fixture-based** (CLI, API, dashboard) — it comes from a synthetic app and deterministic heuristics, not a real model.
- **CLI** (`agentforge evaluate`) submits a run through the API and polls until it finishes; nothing executes in the CLI process.
- **Dashboard** (`apps/web`, Next.js): Applications, Datasets, and a real **Runs** list + **Run detail** page — start a run from a form, watch it go pending → running (live progress) → completed/failed, per-case answers, errors, and every evaluator's score, verdict, reason and evidence. Loading, empty, error, pending, running and failed states are all real. Regression / Trace Explorer / Safety are still disabled nav links, because none of them exists yet.
- **Tests:** 86 automated — 84 Python (unit per evaluator, integration run lifecycle incl. timing-out and crashing cases, raw-SQL trigger tests, CLI → API → Redis → Docker worker end-to-end) and 2 Playwright browser tests (one starts a run from the UI and waits for the worker's completed results). See [Testing](#testing).
- **CI:** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs the Python suite against Postgres + Redis service containers with the worker as a container, on every push and pull request. First run on GitHub (commit `d8851e0`): passed — [run 36768047667](https://github.com/roshano3o3/agentforge/actions/runs/36768047667).

Everything in the CLI output and the dashboard comes from real, persisted rows. Nothing is hardcoded.

## Quick start (Windows)

Prerequisites: Python 3.12+, Node.js 18+, PowerShell, Docker Desktop (WSL2).

```powershell
git clone <this-repo-url> agentforge
cd agentforge

.\scripts\setup.ps1        # venv + all local packages (editable) + npm install
.\scripts\docker-up.ps1    # builds + starts postgres, redis, api (runs migrations), worker
.\scripts\seed-demo.ps1    # publishes datasets/rag_support_v1.yaml, submits a run, waits for it
.\scripts\dev-web.ps1      # dashboard on http://127.0.0.1:3000 (native, not containerized)
```

Then open <http://127.0.0.1:3000/runs>. `.\scripts\docker-down.ps1` stops the stack (`-Volumes` also wipes the Postgres data).

Everything is bound to `127.0.0.1`: Postgres `:5432`, Redis `:6379`, API `:8000`, dashboard `:3000`. The worker publishes no port.

**No-Docker mode (SQLite) is now partial:** `db-migrate.ps1` + `dev-api.ps1` still run the API natively on SQLite, and datasets/applications work, but **runs can't execute** — the worker exists only in Docker (a Linux container can't use a SQLite file on the Windows host), so a run submitted there fails with `503` ("could not be queued") unless Redis and a worker pointed at that same database are running.

## Example evaluation (real output)

`.\scripts\seed-demo.ps1` against the Docker stack — the CLI submits the run, the worker container executes it:

```
Published dataset 'rag-support' version 3 (5 test case(s), id=bf1611b1-d7cc-4840-bc78-6ef46411ae11)
Submitted run 3f84e161-f2f9-40f9-bfe6-f7a0bf74c2c4: rag-assistant@v1 against rag-support v3 (5 case(s)), adapter python:rag_app.adapter:answer
  completed: 5/5 case(s)

5 case(s): 0 passed, 5 ok, 0 error, 0 timeout - pass_rate=0% (threshold=0.7)
latency p50=0.1ms p95=0.1ms (ok cases only, nearest-rank)
estimated cost (ESTIMATED from config/pricing.yaml): 0.000000 USD over 5 case(s); 0 without an estimate
                               Per-metric means
┌───────────────────────────────┬────────────┬──────────────┬───────────┬─────┐
│ Evaluator                     │ Mean score │ Mean value   │ Pass rate │ n/a │
├───────────────────────────────┼────────────┼──────────────┼───────────┼─────┤
│ answer_contains@1.0.0         │ 1.000      │ -            │ 100%      │ 3   │
│ answer_regex@1.0.0            │ 1.000      │ -            │ 100%      │ 3   │
│ citation_correctness@1.0.0    │ 1.000      │ -            │ 100%      │ 0   │
│ estimated_cost@1.0.0          │ -          │ 0 usd        │ -         │ 0   │
│ exact_match@1.0.0             │ 0.000      │ -            │ 0%        │ 0   │
│ heuristic_context_precision@… │ 0.500      │ -            │ 20%       │ 0   │
│ heuristic_context_recall@1.0… │ 1.000      │ -            │ 100%      │ 1   │
│ latency@1.0.0                 │ -          │ 0.0618554 ms │ -         │ 0   │
│ token_usage@1.0.0             │ -          │ 102 tokens   │ -         │ 0   │
└───────────────────────────────┴────────────┴──────────────┴───────────┴─────┘
fixture-based: produced by the local-deterministic provider (synthetic app + deterministic heuristics). Not model quality, and not an LLM judgment.
```

(Per-case table trimmed.) How to read it — all **fixture-based**:

- **Case pass rate is 0%** because a case passes only if *no* evaluator fails it, and `exact_match` fails every case: the dataset's `expected_answer`s are full reference sentences the example app never reproduces verbatim. That is `exact_match` working as defined, not a bug; pick evaluators with `--evaluators` (or the dashboard's checkboxes) when a metric doesn't fit your dataset.
- `heuristic_context_precision` is **identical to Phase 1** (mean 0.500, 1 of 5 at ≥ 0.7): same formula, now executed by the worker.
- Tokens are whitespace word counts the example adapter reports honestly for a model-less fixture, and its model `local-deterministic` is priced at $0 in `config/pricing.yaml` — so the cost really is $0, and still labeled estimated.
- Latency is sub-millisecond because the example "app" is a keyword lookup.

Fault containment, with `datasets/fault_injection_demo.yaml` and the deliberately faulty adapter:

```
agentforge evaluate --app fault-demo --app-version v1 --dataset fault-injection-demo --adapter rag_app.fault_injection:answer --timeout 1 --evaluators heuristic_context_precision,answer_regex,latency
  running: 2/5 case(s)
  completed: 5/5 case(s)
│ calls-sys-exit     │ error   │ FAIL   │          1.4 │
│ good-case          │ ok      │ PASS   │          0.1 │
│ hangs-past-timeout │ timeout │ FAIL   │       1000.9 │
│ malformed-output   │ error   │ FAIL   │          1.0 │
│ raises-exception   │ error   │ FAIL   │          0.9 │
  calls-sys-exit error: SystemExit: fault injection: adapter called sys.exit
  hangs-past-timeout timeout: timeout: adapter did not return within the per-case timeout of 1s
  malformed-output error: agentforge_worker.adapters.AdapterOutputError: adapter returned int; expected AdapterOutput, dict, or str
  raises-exception error: RuntimeError: fault injection: adapter crashed on purpose
5 case(s): 1 passed, 1 ok, 3 error, 1 timeout - pass_rate=20% (threshold=0.7)
```

The worker container kept running (`RestartCount=0`) and a raw `UPDATE evaluation_runs SET status='running'` on that completed run was rejected by Postgres: `ERROR: evaluation run 705dad19-… is completed and immutable`.

## Dataset version lifecycle

Unchanged from Phase 1: a `DatasetVersion` is a **draft** (edit test cases freely) until **published**, then frozen forever — enforced by the service layer (`409` on PATCH) and independently by DB triggers. "Editing" a published version means `POST .../new-draft`. Runs may only target published versions (`400` otherwise), so a run's dataset can never change under it. Test cases gained two optional fields in Phase 2 — `expected_answer_contains` (list of phrases) and `expected_answer_regex` (validated on the way in) — for the answer-match evaluators.

Screenshots from the real Playwright run, in `docs/screenshots/`: [`runs.png`](docs/screenshots/runs.png), [`run-new-form.png`](docs/screenshots/run-new-form.png), [`run-detail-completed.png`](docs/screenshots/run-detail-completed.png), [`applications.png`](docs/screenshots/applications.png), [`dataset-draft-editing.png`](docs/screenshots/dataset-draft-editing.png), [`dataset-published-locked.png`](docs/screenshots/dataset-published-locked.png), [`dataset-v2-from-v1.png`](docs/screenshots/dataset-v2-from-v1.png).

## Architecture

See [`docs/architecture.md`](docs/architecture.md): component diagram, run lifecycle and state machine, domain model, immutability guarantees and how each is enforced, and the real bugs found along the way.

## Testing

```powershell
.\scripts\test.ps1             # Python, no Docker: SQLite; runs executed in-process by the worker's own code
.\scripts\test.ps1 -Postgres   # Python against Docker: Postgres (agentforge_test) + Redis DB 1 + a test worker container
.\scripts\test-ui.ps1          # Playwright (always Docker): Postgres (agentforge_e2e_test) + Redis DB 2 + an e2e worker container
```

Run `docker-up.ps1` first for the Docker modes. The test scripts rebuild the worker image (cached) so the test worker runs the checked-out code, use their own databases and Redis DB indexes, and remove their worker containers afterwards — the dev database and dev queue (Redis DB 0) are never touched.

- **Unit** (`tests/unit`): every evaluator (including not-applicable and never-guess paths for tokens/cost), the registry, pricing parsing, and aggregation/percentiles.
- **Integration** (`tests/integration`): the run lifecycle through the real API with the worker's real `execute_run` — pending + enqueued, draft/unknown-evaluator/malformed-adapter rejection, unreachable queue → run marked failed + `503`, full completion with every evaluator, **a timing-out case and crashing cases** (`RuntimeError`, `SystemExit`, malformed output) recorded while the run completes, finished-run immutability in the service layer, restart after a dead worker, unimportable adapter → failed run with reason, and the HTTP adapter contract against a real local HTTP server. Plus datasets/applications as before.
- **DB triggers** (`test_db_triggers.py`, Postgres mode only — the SQLite test schema comes from `create_all`, which has no triggers): raw SQL bypassing API and ORM. Published test cases can't be inserted/updated/deleted or un-published; a completed run can't be updated or deleted, and its results and metric scores can't be inserted, updated or deleted; a control shows a *running* run is writable.
- **E2E** (`tests/e2e`, needs `-Postgres`): the real CLI (subprocess) → uvicorn API → Redis → the worker **container** → Postgres, for the example dataset and the fault-injection dataset (and a follow-up run proving the worker survived).
- **Browser** (`apps/web/e2e`): the dataset lifecycle (draft → edit → publish → locked with a real `409` → new version), and **starting a run from the UI**, following it to `completed`, and checking per-case verdicts and reasons.

**Note:** Next.js's dev server holds a lock per project directory — stop `dev-web.ps1` before `test-ui.ps1`.

Last run in this environment (Python 3.12.7, Windows 11, Docker Desktop 29.8.1, postgres:16-alpine, redis:7-alpine):

| Suite | Result |
|---|---|
| `test.ps1` (SQLite) | 75 passed, 9 skipped (7 trigger tests, 2 worker e2e tests) |
| `test.ps1 -Postgres` | 84 passed |
| `test-ui.ps1` | 2 passed |
| CI workflow (GitHub Actions, ubuntu) | passed on `d8851e0`: Postgres + worker-container step and SQLite step |

The Phase 2 migration was also checked by hand on both engines against Phase 1-shaped data (a scored run and a stuck `running` run): upgrade carried the scores over as `heuristic_context_precision@1.0.0` metric rows labeled fixture-based and marked the stuck run failed; all new triggers blocked; the dataset triggers survived; downgrade → upgrade round-tripped. No coverage percentage is claimed because none has been measured.

## Repository layout

```
apps/api/            FastAPI backend: models, Alembic migrations, routers, run state machine (services/runs.py)
apps/worker/         arq worker: adapter invocation w/ timeouts (adapters.py), run execution (runner.py) -- Docker only
apps/web/            Next.js dashboard + e2e/ (Playwright)
packages/core/       Shared Pydantic request/response schemas (api, cli, worker)
packages/evaluators/ Evaluator registry, evaluators, pricing parser, aggregation (zero dependencies)
packages/sdk/        Adapter contract (python + http) + AgentForgeClient (httpx only)
cli/                 `agentforge` CLI (Typer)
examples/rag_app/    Synthetic RAG app: adapter, fault-injection adapter, stdlib HTTP adapter server
config/pricing.yaml  Per-model rates for estimated cost (no vendor prices shipped)
datasets/            rag_support_v1.yaml, fault_injection_demo.yaml
tests/               unit / integration / e2e (Python)
.github/workflows/   ci.yml
docs/                architecture.md, evaluators.md, screenshots/
scripts/             PowerShell setup/dev/test/docker scripts
```

## Current limitations

Read this before assuming a feature exists.

- **No authentication.** None, anywhere. Everything is bound to `127.0.0.1` because of that. **Not ready for any public or shared deployment.**
- **Adapters are trusted local code, not sandboxed.** A `python` adapter runs inside the worker process with the worker's full permissions; an `http` adapter can point the worker at any URL. The timeout and exception capture contain *bugs*, not malicious code.
- **A timed-out synchronous adapter keeps running.** Python can't kill a thread: the case is recorded as `timeout` and the run moves on immediately, but the stuck call continues in a background daemon thread until it returns or the worker restarts. Async adapters are cancelled properly, unless they block the event loop.
- **Python adapters must be installed in the worker image** (`apps/worker/Dockerfile`). Today that's only the example app. From inside the container, a service on your host is `http://host.docker.internal:<port>`.
- **Cases run sequentially within a run**; up to 4 runs execute concurrently per worker (`AGENTFORGE_WORKER_MAX_JOBS`). A whole run must finish within `AGENTFORGE_JOB_TIMEOUT_SECONDS` (default 3600) or it's marked failed.
- **A run interrupted by a worker shutdown is marked failed**, not resumed. A hard-killed worker (no chance to clean up) gets one arq retry, which restarts that run from scratch.
- **All evaluators are deterministic string/ID heuristics.** No LLM-as-judge, no semantic similarity, no RAGAS. `heuristic_context_*` are ID-overlap heuristics, not the RAGAS metrics of similar names.
- **Token counts and model names are only what the adapter reports**; cost is only an estimate, and only for models listed in `config/pricing.yaml` (which ships with none but the $0 fixture).
- **Case pass/fail is strict**: any failing evaluator fails the case. There is no per-evaluator weighting or gate policy yet (Phase 4).
- **No-Docker (SQLite) mode can't execute runs** (see Quick start). The SQLite path remains for the API, datasets, and the in-process test suite.
- **The full stack incl. the browser suite is verified on one Windows machine only.** CI (Linux) runs the Python suite only — no Playwright in CI yet.
- **No release gate, regression comparison, safety/adversarial testing, trace persistence, or agent trajectory evaluation** — later phases.
- **A known SQLite-only quirk:** timestamps re-read from SQLite can lose their UTC-offset suffix (same instant). Postgres doesn't.
- **A draft PATCH replaces the entire test-case set**, not a partial merge.
- **`agentforge.yaml` (from `agentforge init`) is not read by anything yet.**

## What's next

Phase 3 (agent trajectory evaluation, LangGraph example agent), Phase 4 (regression engine, release policy, CI gate), Phase 5 (safety/adversarial testing), Phase 6 (OpenTelemetry tracing), Phase 7 (failure replay), remaining dashboard pages, then a reproducible benchmark. Not started; not claimed as done.

## License

[MIT](LICENSE)
