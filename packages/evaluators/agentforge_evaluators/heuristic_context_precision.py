"""A single deterministic evaluator: document-ID overlap as a proxy for
RAG "context precision".

This module intentionally has zero third-party dependencies. It operates on
plain Python lists of strings so it can run client-side in the CLI (Phase 1,
synchronous) or later inside an async worker (Phase 2+) without pulling in
SQLAlchemy, an HTTP client, or a network dependency of any kind.

IMPORTANT — what this metric is and is not:

* It is NOT the RAGAS `context_precision` metric. RAGAS's metric is
  rank-weighted (it rewards relevant chunks appearing earlier in the
  retrieved list) and, in its default configuration, uses an LLM judge to
  decide whether each retrieved chunk is relevant to the question.
* It is NOT an LLM-as-judge score of any kind. No model call is made.
* It IS a simple, fully deterministic, order-agnostic set-overlap heuristic:
  of the documents actually retrieved, what fraction appear on the
  test case's hand-labeled `expected_relevant_doc_ids` list.

Everywhere this metric is surfaced (CLI output, API fields, the frontend,
and docs) it must be labeled "heuristic context precision", never plain
"context precision", so it is never confused with the full RAGAS metric or
an LLM-judge result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EVALUATOR_NAME = "heuristic_context_precision"
EVALUATOR_VERSION = "1.0.0"


@dataclass
class HeuristicContextPrecisionResult:
    score: float
    evidence: dict = field(default_factory=dict)


def heuristic_context_precision(
    retrieved_doc_ids: list[str],
    expected_relevant_doc_ids: list[str],
) -> HeuristicContextPrecisionResult:
    """Compute heuristic context precision for one test case.

    Formula (order-agnostic set overlap):

        score = |retrieved ∩ expected_relevant| / |retrieved|

    In words: of the documents the retriever actually returned, what
    fraction were on the expected-relevant list for this question.

    Edge case — zero documents retrieved:
        The formula above divides by |retrieved|, which is undefined when
        no documents were retrieved at all. We define this case explicitly
        as `score = 0.0` (retrieving nothing is scored as a full miss, not
        skipped and not treated as perfect), and the evidence dict marks
        `no_documents_retrieved: True` so this is visibly distinguishable
        from "documents were retrieved but none were relevant" (which also
        scores 0.0, but with `retrieved_count > 0`).

    Returns a result whose `evidence` dict contains every input needed to
    understand *why* the score came out the way it did, for display in the
    CLI/API/frontend.
    """
    retrieved = list(retrieved_doc_ids)
    expected = set(expected_relevant_doc_ids)
    retrieved_set = set(retrieved)
    overlap = retrieved_set & expected

    if len(retrieved) == 0:
        evidence = {
            "formula": "|retrieved ∩ expected_relevant| / |retrieved|",
            "retrieved_doc_ids": retrieved,
            "expected_relevant_doc_ids": sorted(expected),
            "overlap_doc_ids": [],
            "retrieved_count": 0,
            "relevant_retrieved_count": 0,
            "no_documents_retrieved": True,
            "note": (
                "No documents were retrieved for this input; precision is "
                "defined as 0.0 in this case rather than undefined."
            ),
        }
        return HeuristicContextPrecisionResult(score=0.0, evidence=evidence)

    score = len(overlap) / len(retrieved)
    evidence = {
        "formula": "|retrieved ∩ expected_relevant| / |retrieved|",
        "retrieved_doc_ids": retrieved,
        "expected_relevant_doc_ids": sorted(expected),
        "overlap_doc_ids": sorted(overlap),
        "retrieved_count": len(retrieved),
        "relevant_retrieved_count": len(overlap),
        "no_documents_retrieved": False,
    }
    return HeuristicContextPrecisionResult(score=score, evidence=evidence)
