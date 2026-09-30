"""Deterministic retrieval / citation evaluators over document IDs.

Like `heuristic_context_precision`, these are set-overlap heuristics over
hand-labeled document IDs -- not the RAGAS metrics of similar names and not
LLM judgments of relevance.

Per-case param for all three: `expected_context: [doc id, ...]` (default: the
case's own expected_context).
"""

from __future__ import annotations

from agentforge_evaluators.base import EvalConfig, EvalInput, MetricOutcome, Params, not_applicable
from agentforge_evaluators.heuristic_context_precision import heuristic_context_precision


def _expected(case: EvalInput, params: Params) -> list[str]:
    return list(params.get("expected_context", case.expected_context))


def context_precision(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    result = heuristic_context_precision(case.retrieved_doc_ids, _expected(case, params))
    passed = result.score >= config.threshold
    if result.evidence["no_documents_retrieved"]:
        reason = "no documents retrieved (defined as 0.0)"
    else:
        reason = (
            f"{result.evidence['relevant_retrieved_count']} of {result.evidence['retrieved_count']} "
            f"retrieved doc(s) are expected-relevant"
        )
    reason += f"; {'>=' if passed else '<'} threshold {config.threshold}"
    return MetricOutcome(score=result.score, passed=passed, reason=reason, evidence=result.evidence)


def context_recall(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    expected = set(_expected(case, params))
    if not expected:
        return not_applicable("no expected context for this case, so recall is undefined")
    retrieved = set(case.retrieved_doc_ids)
    found = retrieved & expected
    score = len(found) / len(expected)
    passed = score >= config.threshold
    return MetricOutcome(
        score=score,
        passed=passed,
        reason=(
            f"retrieved {len(found)} of {len(expected)} expected-relevant doc(s); "
            f"{'>=' if passed else '<'} threshold {config.threshold}"
        ),
        evidence={
            "formula": "|retrieved ∩ expected_relevant| / |expected_relevant|",
            "retrieved_doc_ids": list(case.retrieved_doc_ids),
            "expected_relevant_doc_ids": sorted(expected),
            "found_doc_ids": sorted(found),
            "missed_doc_ids": sorted(expected - retrieved),
        },
    )


def citation_correctness(case: EvalInput, config: EvalConfig, params: Params) -> MetricOutcome:
    """Of the documents the answer cites, what fraction are expected-relevant.

    A citation to a document that wasn't even retrieved is "unsupported" (the
    answer cites something it never saw) and fails the case regardless of
    score. Citing nothing is correct only when nothing was expected.
    """
    cited = list(dict.fromkeys(case.citations))  # de-duplicate, keep order
    expected = set(_expected(case, params))
    retrieved = set(case.retrieved_doc_ids)

    if not cited:
        if not expected:
            return MetricOutcome(
                score=1.0,
                passed=True,
                reason="answer cites no documents and none were expected",
                evidence={"cited_doc_ids": [], "expected_relevant_doc_ids": []},
            )
        return MetricOutcome(
            score=0.0,
            passed=False,
            reason=f"answer cites no documents but {len(expected)} were expected",
            evidence={"cited_doc_ids": [], "expected_relevant_doc_ids": sorted(expected)},
        )

    correct = [d for d in cited if d in expected]
    unsupported = [d for d in cited if d not in retrieved]
    score = len(correct) / len(cited)
    passed = score >= config.threshold and not unsupported
    reason = f"{len(correct)} of {len(cited)} citation(s) are expected-relevant"
    if unsupported:
        reason += f"; {len(unsupported)} cite(s) a document that was never retrieved: {unsupported}"
    reason += f"; {'>=' if score >= config.threshold else '<'} threshold {config.threshold}"
    return MetricOutcome(
        score=score,
        passed=passed,
        reason=reason,
        evidence={
            "formula": "|cited ∩ expected_relevant| / |cited|; any cited-but-not-retrieved doc fails the case",
            "cited_doc_ids": cited,
            "expected_relevant_doc_ids": sorted(expected),
            "retrieved_doc_ids": list(case.retrieved_doc_ids),
            "correct_citations": correct,
            "unsupported_citations": unsupported,
        },
    )
