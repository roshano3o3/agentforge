# Evaluators

Every evaluator is **deterministic Python** in `packages/evaluators` (zero
third-party dependencies). **None is an LLM judge, and none calls a model.**
They check strings, regexes, document IDs, and measured numbers against
what the dataset author wrote down.

## Contract

Each evaluator is registered as `name@version` (`registry.py`) and returns,
per test case:

| Field | Meaning |
|---|---|
| `score` | 0..1 for quality evaluators; null for measurement evaluators |
| `value` + `unit` | measured number for measurement evaluators (`ms`, `tokens`, `usd`) |
| `passed` | `true`/`false`; `null` = not applicable (or no budget configured) |
| `reason` | one human-readable sentence explaining the verdict |
| `evidence` | every input needed to re-derive the result without re-running |
| `labels` | `estimated` (cost); the worker adds `fixture-based` to every result of a `local-deterministic` run |

A run pins the exact `name@version` of every evaluator it uses, and every
stored result records the version that produced it. Changing an evaluator's
behavior means registering a new version, never editing one in place.

**Not applicable ≠ failed.** When a case lacks what an evaluator needs
(e.g. no `expected_answer` for `exact_match`), the result is `score=null,
passed=null` with a reason starting "not applicable:", it doesn't affect the
case verdict, and it's counted separately (`not_applicable_count`) rather
than averaged in.

**Case verdict:** a case passes when the adapter call succeeded (`ok`) and
no evaluator returned `passed=false`. Errors and timeouts fail the case and
are not scored. An evaluator that itself raises is recorded as a failed
metric with the exception in its reason ("evaluator error: ..."), not a crash.

`threshold` (default 0.7) is the run's pass line for 0..1 scores.

## Answer text

### `exact_match@1.0.0`
`normalize(answer) == normalize(expected_answer)`, where normalize =
trim + casefold + collapse internal whitespace. Nothing else (punctuation
counts). Score 1.0 / 0.0; passes on 1.0. N/A without `expected_answer`.
A correct answer phrased differently fails — that's the definition.

### `answer_contains@1.0.0`
For each phrase in `expected_answer_contains`: case-insensitive substring
of the normalized answer. Score = found / total; passes only if **all** are
found; evidence lists found and missing. N/A without phrases.

### `answer_regex@1.0.0`
`re.search(expected_answer_regex, answer)` (use `(?i)` for
case-insensitive). Score 1.0 / 0.0. The pattern is validated when the
dataset is written; a pattern that somehow fails to compile fails the case
visibly. N/A without a pattern.

## Retrieval and citations (document IDs)

### `heuristic_context_precision@1.0.0`
```
score = |retrieved ∩ expected_relevant| / |retrieved|
```
`retrieved` includes duplicates in the denominator; `expected_relevant` is
the case's `expected_context`. **Zero documents retrieved is defined as
0.0** (not undefined, not skipped), with `no_documents_retrieved: true` in
the evidence to distinguish it from "retrieved, none relevant". Passes at
`score >= threshold`.

This is **not** the RAGAS `context_precision` metric: RAGAS is rank-weighted
and by default uses an LLM to judge relevance. This is an order-agnostic
set-overlap heuristic over hand-labeled IDs, which is why it's always
labeled "heuristic". Known limitation: it can't reward correctly
retrieving nothing for an out-of-scope question (that still scores 0.0) —
`rag_support_v1.yaml`'s `out-of-scope-sponsorship-001` shows it.
Unchanged from Phase 1 (same function, `heuristic_context_precision.py`).

### `heuristic_context_recall@1.0.0`
```
score = |retrieved ∩ expected_relevant| / |expected_relevant|
```
N/A when `expected_context` is empty (recall of nothing is undefined).
Passes at `score >= threshold`. Also not the RAGAS metric of that name.

### `citation_correctness@1.0.0`
Citations are the doc IDs the adapter says its answer cites (de-duplicated).
```
score = |cited ∩ expected_relevant| / |cited|
```
A citation to a document that **wasn't retrieved** is "unsupported" and
fails the case regardless of score. No citations: passes (1.0) only if
nothing was expected, otherwise fails (0.0). Passes at `score >= threshold`
with no unsupported citations.

## Measurements

### `latency@1.0.0`
Wall-clock time of the adapter call, measured in the worker, in ms. With a
`max_latency_ms` budget on the run: passes at `latency <= budget`;
without one, `passed` is null (recorded, not judged).

### `token_usage@1.0.0`
`input_tokens + output_tokens` **as reported by the adapter**. AgentForge
never estimates or guesses token counts: if the adapter reports none, the
value is null with reason "not reported by adapter"; if only one side is
reported, evidence marks it `partial`. The example RAG app reports
whitespace word counts, because it calls no model.

### `estimated_cost@1.0.0` — always labeled `estimated`
```
usd = input_tokens / 1e6 * input_rate + output_tokens / 1e6 * output_rate
```
Rates come from `config/pricing.yaml`, keyed by the model name the adapter
reports. No rate is ever invented: with no reported tokens, no reported
model, or a model missing from the pricing file, the value is null and the
reason says which. The repo ships **no vendor prices** (they'd silently go
stale) — only `local-deterministic` at $0, which is accurate for the
model-less example app. Add your models' current rates yourself; the worker
reads the file at startup.

## Run aggregates

Computed once from the persisted rows when a run completes
(`aggregate.py`) and stored on the run:

- `pass_rate` = passed cases / all cases (errors and timeouts count as not passed)
- `latency_ms.p50` / `p95`: **nearest-rank** percentile (always an observed
  value, no interpolation) over **ok cases only** — a timeout's latency is
  just the configured timeout, not the app's latency
- per evaluator: `mean_score` (non-null scores), `mean_value` (non-null
  values), `pass_rate` (non-null verdicts), counts of scored / valued / not-applicable
- `estimated_cost_usd`: total over cases with an estimate, plus how many had
  none (never treated as $0)

## Tests

`tests/unit/test_evaluators.py` (each evaluator's pass, fail and N/A paths,
never-guess paths for tokens and cost, registry, pricing parsing),
`tests/unit/test_aggregate.py`, and `tests/unit/test_heuristic_context_precision.py`.
