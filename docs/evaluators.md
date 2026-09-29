# Evaluators

Phase 1 ships exactly **one** evaluator. This document defines it precisely
so nothing about it is ambiguous or overstated.

## `heuristic_context_precision`

**What it is:** a deterministic, order-agnostic set-overlap heuristic over
document IDs. It measures: of the documents an application's retriever
actually returned for a test case, what fraction were on that test case's
hand-labeled `expected_context` (relevant document IDs) list.

**What it is *not*:**

- It is **not** the RAGAS `context_precision` metric. RAGAS's version is
  rank-weighted (relevant chunks ranked earlier score better) and, in its
  default configuration, uses an LLM judge to decide whether each retrieved
  chunk is actually relevant to the question. This evaluator does neither.
- It is **not** an LLM-as-judge score of any kind. No model call is made —
  Phase 1 uses the `local-deterministic` provider exclusively.
- It does **not** evaluate the generated answer text at all, only the
  retrieved document IDs.

Everywhere this metric appears — CLI output, API fields, the frontend, run
metadata — it is labeled `heuristic_context_precision`, never plain "context
precision," so it can't be mistaken for the full RAGAS metric.

### Formula

```
score = |retrieved ∩ expected_relevant| / |retrieved|
```

Where `retrieved` is the list of document IDs the adapter returned for a
test case (duplicates included — the denominator is `len(retrieved)`, not
the size of the deduplicated set) and `expected_relevant` is the test case's
`expected_context` list.

### Edge case: zero documents retrieved

The formula above divides by `|retrieved|`, which is undefined when nothing
was retrieved. This is defined explicitly, not left to divide-by-zero
behavior:

> **When `retrieved` is empty, `score = 0.0`.**

Retrieving nothing is scored as a full miss, not skipped and not treated as
a perfect score. The evidence returned alongside the score sets
`no_documents_retrieved: true` in this case, which is how a real "nothing
retrieved" outcome is distinguished from "retrieved documents, but none of
them were relevant" (which also scores `0.0`, but with
`no_documents_retrieved: false` and `retrieved_count > 0`).

**Known limitation:** this formula cannot reward the case where the
*correct* behavior is to retrieve nothing at all (e.g. a genuinely
out-of-scope question with an empty `expected_context`). Such a case still
scores `0.0`, identical to a real miss. `datasets/rag_support_v1.yaml`'s
`out-of-scope-sponsorship-001` case exercises exactly this — see
`docs/benchmarks` / the README for the real, persisted result.

### Evidence

Every scored result stores the exact inputs to the formula, so a score can
always be explained without re-running anything:

```json
{
  "formula": "|retrieved ∩ expected_relevant| / |retrieved|",
  "retrieved_doc_ids": ["policy-returns-001", "policy-international-005"],
  "expected_relevant_doc_ids": ["policy-returns-001"],
  "overlap_doc_ids": ["policy-returns-001"],
  "retrieved_count": 2,
  "relevant_retrieved_count": 1,
  "no_documents_retrieved": false
}
```

### Implementation

`packages/evaluators/agentforge_evaluators/heuristic_context_precision.py`.
Zero third-party dependencies — pure Python — so it can run client-side in
the CLI (Phase 1) or, unchanged, inside an async worker once one exists
(Phase 2+). See `tests/unit/test_heuristic_context_precision.py` for the
formula's test coverage, including the zero-retrieval and zero-overlap
branches.

### Versioning

`EVALUATOR_NAME = "heuristic_context_precision"`,
`EVALUATOR_VERSION = "1.0.0"`. Every persisted `EvaluationResult` and
`EvaluationRun` records both, so a future change to the formula (a version
bump) never silently reinterprets historical results — old runs keep
pointing at the evaluator version that actually produced them.

## What's not implemented yet

RAG evaluators beyond this one (faithfulness, answer relevance, citation
correctness, groundedness), agent trajectory evaluators, safety/adversarial
evaluators, and any LLM-as-judge evaluator are all out of scope for Phase 1.
See the root README's "What's next" section.
