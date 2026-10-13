# AgentForge

[![CI](https://github.com/roshano3o3/agentforge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/roshano3o3/agentforge/actions/workflows/ci.yml)

**Open-source evaluation and release-gating platform for production AI agents. Test RAG quality, agent trajectories, safety, latency and cost before shipping.**

![AgentForge demo: runs list, a FAILED regression of v1 vs v2, the Safety dashboard, a failing injection case's trace with delete_invoice highlighted, and a replay with defense D2 back on going from FAIL to PASS](docs/demo.gif)

*Recorded from the seeded demo (`docker compose up`) by [`apps/web/demo/capture.spec.ts`](apps/web/demo/capture.spec.ts); regenerate with [`scripts/demo-capture.ps1`](scripts/demo-capture.ps1). The example agent is scripted, so its results are fixture-based (see [Limitations](#limitations)).*

## What it catches

Three pull requests are left open on purpose, never to be merged. Each one runs the release gate in GitHub Actions against `main`:

- **A broken agent refactor.** [PR #3](https://github.com/roshano3o3/agentforge/pull/3) switches the invoice agent to "v2": refunds under $100 skip approval, a denied approval is ignored, and the agent deletes invoices instead of voiding them. The gate **fails**: trajectory 3 of 8 checks, safety 4 of 11. Every failure names the tool call that broke the rule, e.g. "issue_refund called at step 3 after request_human_approval at step 2 was denied".
- **A "harmless" performance change that opens a prompt injection.** [PR #4](https://github.com/roshano3o3/agentforge/pull/4) turns off one defense: treating tool output as data. The trajectory gate still **passes** (8 of 8). The safety gate **fails** on injection only: `injection_indirect` drops to 0.2, because an instruction hidden in an invoice's memo made the agent call `delete_invoice`.
- **A change that is actually safe passes.** [PR #2](https://github.com/roshano3o3/agentforge/pull/2) refactors the agent without changing its behaviour. The gate **passes**: 8 of 8 trajectory checks and 11 of 11 safety checks.
- **The failure can be explained and the fix proven without redeploying.** Open PR #4's failing case in the Trace Explorer to see the stored span tree with `delete_invoice` blamed. Then replay just that case with the defense back on: **FAIL → PASS**, with `delete_invoice` gone from the trajectory.

## Architecture

```mermaid
flowchart LR
    subgraph clients [Clients]
        CLI["agentforge CLI"]
        WEB["Dashboard<br/>(Next.js)"]
        GHA["GitHub Actions<br/>release gate"]
    end
    subgraph core ["AgentForge (Docker Compose)"]
        API["API<br/>(FastAPI)"]
        Q[("Redis<br/>arq queue")]
        W["Worker"]
        EV["Evaluators<br/>deterministic, name@version"]
        PG[("Postgres<br/>runs, steps, spans,<br/>decisions: immutable")]
    end
    AGENT["Your agent<br/>SDK adapter: python or http"]
    LLM["LLM providers<br/>Anthropic, OpenAI, Ollama"]
    OTEL["OTel collector / Jaeger<br/>(optional)"]

    GHA -->|"agentforge gate"| CLI
    CLI --> API
    WEB --> API
    API -->|"enqueue run"| Q --> W
    W -->|"one call per test case"| AGENT
    AGENT -.->|"LLM planner only"| LLM
    W --> EV
    EV -->|"verdicts, scores, reasons"| PG
    W -->|"results, steps, spans"| PG
    API --> PG
    API -.->|OTLP| OTEL
    W -.->|OTLP| OTEL
```

The API validates, stores and queues. Only the worker runs agents and evaluators, and it does so in a Linux container. The agent tells AgentForge what it did through a small adapter contract (its answer, plus the steps it took). Details: [`docs/architecture.md`](docs/architecture.md).

## Why AgentForge

An agent's final answer can look right even though it got there wrongly: refunding without approval, deleting a record it should have voided, or obeying an instruction it found in a tool result. Most evaluation tools score the text. AgentForge scores **what the agent did**, and blocks the merge when that gets worse:

- **Trajectory-aware.** Every tool call, argument and result the agent reports is stored and checked against the rules the dataset declares.
- **Safety as a regression test.** Attack variants are generated from your own test cases, across seven categories. The pass rate per category is gated like any other metric.
- **Reproducible and immutable.** Datasets are versioned and frozen once published. Runs pin their evaluator versions. Finished runs, release decisions and traces are protected by database triggers, not just by the application.
- **Explainable failures.** A failing case links to its trace and can be replayed with one setting changed, then diffed step by step against the original.
- **No invented numbers.** Every evaluator is a deterministic rule; none is an LLM judge. Cost is always labelled *estimated*. Results from the scripted example agent are labelled *fixture-based*. Every number in this README is listed with its source in [`docs/results.md`](docs/results.md).

## Quick Start

Prerequisites: Docker (Docker Desktop on Windows/macOS) and Git. No API keys are needed.

```bash
git clone https://github.com/roshano3o3/agentforge.git
cd agentforge
cp .env.example .env          # Windows (cmd/PowerShell): copy .env.example .env
docker compose up
```

The first build takes a few minutes. Compose then starts:

- **Infrastructure:** Postgres, Redis, the API (it runs its migrations on start) and the worker.
- **Dashboard:** on <http://127.0.0.1:3000>.
- **`seed`:** a one-shot container that runs [`agentforge demo seed`](cli/agentforge_cli/demo.py). It loads, through the API:
  - the datasets;
  - a v1 production baseline;
  - the v1, v2 and PR #4 runs, with their release decisions;
  - one replay of PR #4's failing injection case with D2 on.

It prints each result and exits. On later `docker compose up`s it finds data and does nothing.

Open <http://127.0.0.1:3000/runs>. Every port is bound to `127.0.0.1`, because there is no authentication (see [Limitations](#limitations)). `docker compose down -v` removes everything, including the data.

This exact sequence was run on fresh clones (see [results](docs/results.md#fresh-clone-quick-start)). For development (editable packages, the CLI on the host, the dashboard dev server), see [`docs/development.md`](docs/development.md).

## Evaluation metrics

There are 26 registered evaluator versions, and every one is deterministic. A run pins each `name@version` it uses, so old runs keep their verdicts when an evaluator changes.

| Group | Evaluators |
|---|---|
| Answer and retrieval | `exact_match`, `answer_contains`, `answer_regex` (fact-based `@1.1.0`), `heuristic_context_precision`, `heuristic_context_recall`, `citation_correctness` |
| Measurement | `latency`, `token_usage`, `estimated_cost` (reported tokens × [`config/pricing.yaml`](config/pricing.yaml), labelled estimated) |
| Trajectory | `tool_selection`, `forbidden_tool_use`, `sequence_order`, `tool_args`, `approval_required`, `loop_detection`, `step_limit` |
| Safety | `injection_resisted`, `pii_leak`, `graceful_tool_failure` (negation-aware `@1.1.0`), `unauthorized_action_blocked`, `tool_args_valid` |
| Opt-in | `answer_grounded` (every fact in the answer must be supported by a tool result; not in any default set) |

Each dataset version declares its default evaluators, and each case can add or drop checks. Each result stores a score or measured value, pass/fail, a human-readable reason and evidence. Exact definitions: [`docs/evaluators.md`](docs/evaluators.md).

## Agent trajectory evaluation

The adapter reports the agent's steps: each tool call with its arguments and its result or error. Each test case declares what a correct trajectory looks like: expected and forbidden tools, order, required arguments, which tools need approval first, a loop limit and a step budget. Seven trajectory evaluators compare the two and name the step that broke a rule.

The example is [`examples/invoice_agent`](examples/invoice_agent), a real LangGraph `StateGraph` + `ToolNode` with eight tools on a synthetic ledger. "v2" adds five deliberate regressions. On the 10-case [`datasets/invoice_agent_v1.yaml`](datasets/invoice_agent_v1.yaml), **v1 passes 10/10 and v2 passes 4/10**. Two of v2's six failures have final answers that pass every answer check: only the trajectory shows the problem.

![v2's failing refund: step 3 (issue_refund after a denied approval) highlighted with the evaluators' reasons](docs/screenshots/trajectory-v2-failing.png)

More detail, including the per-evaluator table and every failure: [`docs/trajectory.md`](docs/trajectory.md).

## Adversarial & safety testing

`agentforge adversarial generate` derives attack variants from a published dataset and a profile of the app's attack surface, across seven categories:

- **Injection:** direct injection, and indirect injection through tool results.
- **Bad inputs:** poisoned context and malformed tool arguments.
- **Failures and data:** tool failures and PII probes.
- **Permissions:** unauthorized tool use.

It's deterministic: the same seed gives byte-identical output. The attack setup reaches the agent; the test's metadata never does.

On the 35-variant safety dataset (5 per category), **v1 (five simple defenses) passes 35/35 and v2 (three defenses off) passes 15/35**. v1's 35/35 is on the same author's templates, so it shows the mechanics working, not robustness. On a 226-variant stress set (every template on every case, not gated), v1 passes 211/226. The 15 failures are real weaknesses of its keyword planner.

![Safety dashboard: v2 vs v1 per attack category, drilled into injection_indirect with the delete_invoice step highlighted](docs/screenshots/safety-v1-vs-v2.png)

More detail, including the defenses, the stress run and the safety policy: [`docs/safety.md`](docs/safety.md).

## CI/CD release gate

[`agentforge.yaml`](agentforge.yaml) holds two policies:

- **`release_policy`:** pass-rate and latency floors, maximum regressions against the production baseline, and no newly failing `critical` case.
- **`safety_policy`:** a minimum per attack category, and no regression in injection, unauthorized-tool or PII cases.

`agentforge gate` sends a policy to the API. The API computes every check with plain arithmetic over the two stored runs, stores an immutable release decision, and exits 0 (PASSED), 1 (FAILED) or 2 (couldn't evaluate).

[`.github/workflows/agentforge-gate.yml`](.github/workflows/agentforge-gate.yml) runs on every pull request:

1. It evaluates the agent as the base branch has it (the baseline) and as the PR has it (the candidate), on both datasets.
2. It gates each dataset with the **base branch's** policy, so a PR can't loosen its own gate.
3. It posts one PR comment and fails the check on FAILED.

| PR | Change | Trajectory gate | Safety gate |
|---|---|---|---|
| [#2](https://github.com/roshano3o3/agentforge/pull/2) | same behaviour, refactored | **PASSED** 8/8 | **PASSED** 11/11 |
| [#3](https://github.com/roshano3o3/agentforge/pull/3) | v2: five regressions, three defenses off | **FAILED** 3/8 (pass rate 1 → 0.4) | **FAILED** 4/11 |
| [#4](https://github.com/roshano3o3/agentforge/pull/4) | only defense D2 off | **PASSED** 8/8 | **FAILED** 9/11 (injection only) |

![Regression page: v1 baseline vs v2 candidate, run-level and per-evaluator deltas, case classes and the FAILED release decision](docs/screenshots/regression-v1-vs-v2.png)

More detail, including the gate's real output and the policy semantics: [`docs/release-gate.md`](docs/release-gate.md).

## Tracing & failure replay

Every run is traced with OpenTelemetry, from run creation in the API, through the queue and the worker, into the agent's own planner decisions and tool calls. Each case's span tree is stored in Postgres. It is PII-redacted before storage, frozen with the run, and optionally exported over OTLP (a Jaeger profile is included).

The **Trace Explorer** shows the tree as a waterfall, with the spans that failed evaluators blamed marked in red. Tracing costs about +7–10 ms per case on the fixture agent, mostly writing span rows.

**Replay** re-runs one case result with the run's own adapter, dataset version and evaluator versions. It changes only the settings the adapter declares, such as the invoice agent's defenses D1–D5. It then diffs verdicts, trajectory (step by step), answer, latency and tokens against the original, and warns if the worker's code or pricing changed since. Replaying PR #4's failing case with D2 on: **FAIL → PASS**, `injection_resisted` fixed, `delete_invoice` removed.

![Replay of PR #4's failing injection case with D2 on: FAIL → PASS, injection_resisted fixed, delete_invoice removed](docs/screenshots/replay-diff.png)

More detail: [`docs/tracing.md`](docs/tracing.md) (span tree, redaction, measured overhead) and [`docs/replay.md`](docs/replay.md) (override contract, the 400, provenance, diff).

## Benchmarks: real LLMs

The same LangGraph agent with a model as the planner. It has the same tools, scenarios and defenses, runs through the same pipeline, and is scored by the same deterministic evaluators. Pass rates below use the fact-based `@1.1.0` answer checks (Part B's outputs re-scored with no model call). The first two models ran 3 repeats on each dataset:

| Model | Trajectory (10 cases) | Safety (35 cases) |
|---|---|---|
| Claude Haiku 4.5 | 1.00 | 0.86 |
| Claude Sonnet 5.5 | 0.70 | 0.91 |
| Llama 3.1 8B (Ollama, 1 run) | 0.30 | not run |
| scripted v1 (reference) | 1.00 | 1.00 |

- **Repeats.** Pass rates were identical in every repeat.
- **Sonnet's trajectory failures.** Its 3 failures are extra verification lookups, which `tool_selection` counts as wrong (left unchanged on purpose).
- **Haiku.** It is the only model here that an injected instruction got through (Llama didn't run the safety set).
- **Spend.** Estimated **$2.02** for the full comparison and **$0.41** for the Part C fix re-runs.

**Calibration.** The answer checks were calibrated against hand labels: 45 dev, 25 held-out and 30 mutant answers.

- **@1.1.0:** agrees on 44/45 dev (in-sample) and 25/25 held-out. It catches only 11/30 mutants, because it doesn't compare facts the check doesn't name. That miss is reported, not tuned away.
- **`answer_grounded@1.0.0`:** clean on calibration, but produced **14 false positives on real model outputs**. So it ships opt-in, and its 1.1.0 numbers are labelled in-sample.

Everything (per-model failures, defenses as prompt vs code, cost estimates against actual spend, how to reproduce): [`docs/benchmarks.md`](docs/benchmarks.md).

## API

FastAPI, OpenAPI docs at <http://127.0.0.1:8000/docs> when the stack is up. The main routes:

| Area | Routes |
|---|---|
| Applications & datasets | `GET/POST /applications`, `GET/POST /datasets`, draft → `PATCH` → `POST .../publish`, `POST .../new-draft` |
| Runs | `POST /runs` (202, queued), `GET /runs`, `GET /runs/{id}`, `GET /evaluators` |
| Release gate | `PUT/GET /baselines`, `GET /regression`, `POST/GET /release-decisions` |
| Tracing & replay | `GET /traces/{result_id}`, `GET /results/{id}/replay-options`, `POST /replay`, `GET /replays/{id}` |
| Re-score | `POST /runs/{id}/rescores`, `GET /rescores/{id}` |

There is deliberately no endpoint for a client to submit results or scores; only the worker writes them. The CLI (`agentforge evaluate | gate | replay | rescore | compare | adversarial generate | demo seed`) is a thin client over this API.

## Screenshots

All captured from the seeded demo at 1440×900, light theme, by [`capture.spec.ts`](apps/web/demo/capture.spec.ts).

| | |
|---|---|
| [![Runs](docs/screenshots/runs.png)](docs/screenshots/runs.png) Runs | [![New run](docs/screenshots/run-new-form.png)](docs/screenshots/run-new-form.png) New run form |
| [![Run detail](docs/screenshots/run-detail-completed.png)](docs/screenshots/run-detail-completed.png) Run detail (RAG) | [![Trajectory](docs/screenshots/run-detail-trajectory-v2.png)](docs/screenshots/run-detail-trajectory-v2.png) v2 trajectories |
| [![Baselines](docs/screenshots/baselines.png)](docs/screenshots/baselines.png) Baselines | [![Regression](docs/screenshots/regression-v1-vs-v2.png)](docs/screenshots/regression-v1-vs-v2.png) Regression |
| [![Safety](docs/screenshots/safety-v1-vs-v2.png)](docs/screenshots/safety-v1-vs-v2.png) Safety | [![Trace](docs/screenshots/trace-explorer.png)](docs/screenshots/trace-explorer.png) Trace Explorer |
| [![Replay](docs/screenshots/replay-diff.png)](docs/screenshots/replay-diff.png) Replay diff | [![Datasets](docs/screenshots/datasets.png)](docs/screenshots/datasets.png) Datasets |
| [![Published](docs/screenshots/dataset-published-locked.png)](docs/screenshots/dataset-published-locked.png) Published version (locked) | [![Draft](docs/screenshots/dataset-draft-editing.png)](docs/screenshots/dataset-draft-editing.png) Draft version, editing |

Also captured: [`trajectory-v1-passing.png`](docs/screenshots/trajectory-v1-passing.png), [`dataset-v2-from-v1.png`](docs/screenshots/dataset-v2-from-v1.png) and [`applications.png`](docs/screenshots/applications.png). The PR gate screenshots ([`ci-gate-pr3-failed-check.png`](docs/screenshots/ci-gate-pr3-failed-check.png), [`ci-gate-pr3-comment.png`](docs/screenshots/ci-gate-pr3-comment.png), [`ci-gate-pr4-comment.png`](docs/screenshots/ci-gate-pr4-comment.png)) are of GitHub, not the dashboard, so they weren't re-captured.

## Tests

**474 automated tests:** 460 Python (unit, integration against the real API and the worker's code, raw-SQL trigger tests, CLI → API → Redis → Docker worker end-to-end) and 14 Playwright browser tests. They run in CI on every push (lint, Python on Postgres and SQLite, browser).

```powershell
.\scripts\test.ps1             # Python on SQLite (no Docker); runs executed in-process by the worker's code
.\scripts\test.ps1 -Postgres   # Python against Docker: Postgres + Redis + a worker container
.\scripts\test-ui.ps1          # Playwright, always Docker
```

Each suite uses its own database and Redis index, never the dev ones. What each suite covers, and lint commands: [`docs/testing.md`](docs/testing.md).

## Limitations

**No authentication.** Everything binds to `127.0.0.1`; it is not ready for any shared deployment. Adapters are trusted local code, not sandboxed.

**The example agent's planner is scripted Python, not an LLM.**
- Its results (including every PR gate verdict and the demo) are **fixture-based**: they show the mechanics catching real trajectory bugs in a real LangGraph graph, not any model's quality.
- The v2 regressions were written on purpose.
- The same author wrote v1's defenses and the attack templates.

**Every evaluator is a deterministic rule** (string, ID, regex or fact patterns), not an LLM judgment.
- The safety gate has 5 cases per category, so its thresholds can only say "no regression" or "at most one failure".
- PII redaction is regex-based (no names or addresses).

**Labelling bias.** The same person labelled the calibration sets and built the evaluators. The labels were hash-frozen before the evaluators ran on them, but the labeller knew what the checks look for.

**Grounding is opt-in.** `answer_grounded@1.0.0` looked clean on calibration and then produced 14 false positives on real outputs. 1.1.0's numbers are in-sample. It stays out of every default set until it's validated on fresh held-out real outputs.

**The LLM benchmark is small and synthetic.**
- **Size:** one day of measurements: 3 repeats per dataset for the two Claude models, 1 run of the 10-case set for Llama.
- **Tasks:** written for the scripted agent.
- **Hosted models:** they can change behind the same id.
- **Llama 3.1 8B:** ran mostly on CPU on a laptop (58% CPU / 42% GPU), so its latency describes that machine.

**Tested environments.**
- Docker Compose was exercised on one Windows machine. CI runs Linux with service containers.
- Purging production data would need an audited admin command; the dev cleanup bypassed the immutability triggers by hand.

The full list: [`docs/limitations.md`](docs/limitations.md).

## Roadmap

Done:
- Phases 1–7: the evaluation engine, trajectories, the release gate, safety testing, tracing and replay.
- The LLM benchmarks, Parts A–D.
- Phase 8 part A: this README, the demo seed, screenshots and the GIF.

Next (not started, not claimed):
- Kubernetes manifests.
- An MCP adapter.
- A reusable GitHub Action instead of [`scripts/ci-gate.sh`](scripts/ci-gate.sh).
- Validating `answer_grounded` on fresh real outputs.
- The OpenAI models in the benchmark.
- Authentication.

## License

[MIT](LICENSE)
