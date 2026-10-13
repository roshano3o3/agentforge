# Development setup (native dashboard, scripts, SQLite)

*Moved here from the root README in Phase 8. The README's own numbers are sourced in [results.md](results.md).*

## Native development (Windows)

The [Quick Start](../README.md#quick-start) needs only Docker. For working on AgentForge itself (editable packages, the CLI on the host, the dashboard's dev server with hot reload):

Prerequisites: Python 3.12+, Node.js 20.9+ (Next.js 16), PowerShell, Docker Desktop (WSL2).

```powershell
.\scripts\setup.ps1        # venv + all local packages (editable) + npm install; writes .env from .env.example
.\scripts\docker-up.ps1    # docker compose up -d --build: postgres, redis, api (runs migrations), worker,
                           # the dashboard container and the one-shot demo seed (empty database only)
docker compose stop web    # free :3000 for the dev server
.\scripts\dev-web.ps1      # next dev on http://127.0.0.1:3000 (native, hot reload)
```

`.venv\Scripts\agentforge demo seed` loads the demo into an empty database from the host (the compose `seed` service runs the same command), and `.\scripts\seed-demo.ps1` publishes and runs only the RAG example. `.\scripts\docker-down.ps1` stops the stack (`-Volumes` also wipes the Postgres data).

Everything is bound to `127.0.0.1`: Postgres `:5432`, Redis `:6379`, API `:8000`, dashboard `:3000`. The worker publishes no port.

**No-Docker mode (SQLite) is now partial:** `db-migrate.ps1` + `dev-api.ps1` still run the API natively on SQLite, and datasets/applications work, but **runs can't execute** — the worker exists only in Docker (a Linux container can't use a SQLite file on the Windows host), so a run submitted there fails with `503` ("could not be queued") unless Redis and a worker pointed at that same database are running.

## Example evaluation (real output)

`.\scripts\seed-demo.ps1` against the Docker stack — the CLI submits the run, the worker container executes it:

```
Published dataset 'rag-support' version 4 (5 test case(s), id=0d1ee444-0198-4f35-9d03-b333bdd3865a)
Submitted run 1a33922a-57d4-4b71-b8f6-84bec0e7f5fc: rag-assistant@v1 against rag-support v4 (5 case(s)), adapter python:rag_app.adapter:answer
  completed: 5/5 case(s)
┌──────────────────────┬────────┬────────┬──────────────┬─────────────────────┐
│ Case                 │ Status │ Passed │ Latency (ms) │ Failed checks       │
├──────────────────────┼────────┼────────┼──────────────┼─────────────────────┤
│ loyalty-points-retu… │ ok     │ FAIL   │          0.1 │ answer_regex=0.00,  │
│                      │        │        │              │ heuristic_context_… │
│ out-of-scope-sponso… │ ok     │ PASS   │          0.1 │ -                   │
│ refund-policy-001    │ ok     │ FAIL   │          0.1 │ answer_contains=0.… │
│                      │        │        │              │ heuristic_context_… │
│ shipping-time-001    │ ok     │ FAIL   │          0.1 │ heuristic_context_… │
│ warranty-outerwear-… │ ok     │ PASS   │          0.1 │ -                   │
└──────────────────────┴────────┴────────┴──────────────┴─────────────────────┘

5 case(s): 2 passed, 5 ok, 0 error, 0 timeout - pass_rate=40% (threshold=0.7)
latency p50=0.1ms p95=0.1ms (ok cases only, nearest-rank)
estimated cost (ESTIMATED from config/pricing.yaml): 0.000000 USD over 5 case(s); 0 without an estimate
                               Per-metric means
┌─────────────────────────────────┬────────────┬────────────┬───────────┬─────┐
│ Evaluator                       │ Mean score │ Mean value │ Pass rate │ n/a │
├─────────────────────────────────┼────────────┼────────────┼───────────┼─────┤
│ answer_contains@1.0.0           │ 0.500      │ -          │ 50%       │ 0   │
│ answer_regex@1.0.0              │ 0.667      │ -          │ 67%       │ 0   │
│ citation_correctness@1.0.0      │ 1.000      │ -          │ 100%      │ 0   │
│ estimated_cost@1.0.0            │ -          │ 0 usd      │ -         │ 0   │
│ heuristic_context_precision@1.… │ 0.625      │ -          │ 25%       │ 0   │
│ heuristic_context_recall@1.0.0  │ 1.000      │ -          │ 100%      │ 0   │
│ latency@1.0.0                   │ -          │ 0.08593 ms │ -         │ 0   │
│ token_usage@1.0.0               │ -          │ 102 tokens │ -         │ 0   │
└─────────────────────────────────┴────────────┴────────────┴───────────┴─────┘
fixture-based: produced by the local-deterministic provider (synthetic app + deterministic heuristics). Not model quality, and not an LLM judgment.
```

How to read it — all **fixture-based**. Each case in [`datasets/rag_support_v1.yaml`](../datasets/rag_support_v1.yaml) now declares the answer check that fits it, on top of dataset defaults (precision, recall, citations, latency, tokens, cost). No metric definition or threshold changed. **Pass rate: 40% (2 of 5)**, up from Phase 2's 0% for two reasons, both stated in the dataset file:

- `exact_match` is no longer applied: `expected_answer` is a free-text reference, and a verbatim comparison measured phrasing, not correctness. It was failing every case.
- The out-of-scope case no longer runs `heuristic_context_precision`/`recall`: precision is *defined* as 0.0 when nothing is retrieved, so it can't credit correctly retrieving nothing (its documented limitation), and recall is undefined with no expected docs. It's checked instead by a refusal regex and by citing nothing. Keeping precision there would make it fail by construction (pass rate 20%).

The three failures are genuine:

- **refund-policy-001** — the question is about returning boots *without tags*. The synthetic policy requires "the original tags", so the correct answer is **no**. The app answers with the 45-day window and never mentions tags, which reads as "yes". (Writing this check exposed that the dataset's own reference answer said "Yes" — wrong per the policy text. It's corrected in version 4.) Precision is also 0.5: the top-2 retriever brings back an irrelevant second doc.
- **loyalty-points-return-001** — a correct answer must say the policy doesn't cover point reversal on returns; the app just quotes how points are earned. Precision 0.5.
- **shipping-time-001** — the answer is right (`answer_contains` passes), but the retriever returns one irrelevant doc of two (precision 0.5 < 0.7). That's the heuristic measuring real retrieval noise.

Also: tokens are whitespace word counts the example adapter reports honestly for a model-less fixture, its model `local-deterministic` is priced at $0 in `config/pricing.yaml` (so the cost really is $0, still labeled estimated), and latency is sub-millisecond because the example "app" is a keyword lookup.

Fault containment, with `datasets/fault_injection_demo.yaml` and the deliberately faulty adapter:

```
agentforge evaluate --app fault-demo --app-version v1 --dataset fault-injection-demo --adapter rag_app.fault_injection:answer --timeout 1
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

Unchanged from Phase 1: a `DatasetVersion` is a **draft** (edit test cases freely) until **published**, then frozen forever — enforced by the service layer (`409` on PATCH) and independently by DB triggers. "Editing" a published version means `POST .../new-draft`. Runs may only target published versions (`400` otherwise), so a run's dataset can never change under it. A version's evaluator config (`default_evaluators` and each case's `evaluators`) is part of that frozen content.

Versions published in Phase 2 used two per-case fields, `expected_answer_contains` and `expected_answer_regex`, instead of per-case config. They're kept read-only and still honored: those versions have no default config, so every pinned evaluator applies as before, with the legacy fields supplied as `answer_contains`/`answer_regex` params at run time. Their stored rows are never rewritten. A new draft made from such a version converts the fields into explicit per-case config. New data can't use them (`422`: unknown fields are rejected rather than silently dropped).

Screenshots from the real Playwright run, in `docs/screenshots/`: [`runs.png`](screenshots/runs.png), [`run-new-form.png`](screenshots/run-new-form.png), [`run-detail-completed.png`](screenshots/run-detail-completed.png), [`trajectory-v1-passing.png`](screenshots/trajectory-v1-passing.png), [`trajectory-v2-failing.png`](screenshots/trajectory-v2-failing.png), [`run-detail-trajectory-v2.png`](screenshots/run-detail-trajectory-v2.png), [`applications.png`](screenshots/applications.png), [`dataset-draft-editing.png`](screenshots/dataset-draft-editing.png), [`dataset-published-locked.png`](screenshots/dataset-published-locked.png), [`dataset-v2-from-v1.png`](screenshots/dataset-v2-from-v1.png).

## Repository layout

```
apps/api/            FastAPI backend: models, Alembic migrations, routers, run state machine (services/runs.py)
apps/worker/         arq worker: adapter invocation w/ timeouts (adapters.py), run execution (runner.py) -- Docker only
apps/web/            Next.js dashboard + e2e/ (Playwright)
packages/core/       Shared Pydantic request/response schemas (api, cli, worker)
packages/evaluators/ Evaluator registry, evaluators (incl. trajectory), pricing parser, aggregation
packages/sdk/        Adapter contract (python + http) + AgentForgeClient (httpx only)
cli/                 `agentforge` CLI (Typer)
examples/rag_app/    Synthetic RAG app: adapter, fault-injection adapter, stdlib HTTP adapter server
examples/invoice_agent/  Synthetic LangGraph invoice agent: scripted planner (v1 + regressed v2) and an LLM planner (answer_llm)
config/pricing.yaml  Per-model rates for estimated cost (vendor rates dated, with sources)
benchmarks/          compare.yaml (the default model comparison) + `agentforge compare` results JSON
datasets/            rag_support_v1.yaml, fault_injection_demo.yaml, invoice_agent_v1.yaml
tests/               unit / integration / e2e (Python)
.github/workflows/   ci.yml
docs/                architecture.md, evaluators.md, screenshots/
scripts/             PowerShell setup/dev/test/docker scripts
```
