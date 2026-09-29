"""Unit tests for the one Phase 1 evaluator: heuristic_context_precision.

Pure-Python, no DB/API/network involved.
"""

from __future__ import annotations

from agentforge_evaluators import heuristic_context_precision


def test_perfect_precision_when_all_retrieved_docs_are_relevant() -> None:
    result = heuristic_context_precision(
        retrieved_doc_ids=["doc-a", "doc-b"],
        expected_relevant_doc_ids=["doc-a", "doc-b"],
    )
    assert result.score == 1.0
    assert result.evidence["retrieved_count"] == 2
    assert result.evidence["relevant_retrieved_count"] == 2
    assert result.evidence["no_documents_retrieved"] is False


def test_partial_precision_with_one_relevant_and_one_irrelevant() -> None:
    result = heuristic_context_precision(
        retrieved_doc_ids=["doc-a", "doc-irrelevant"],
        expected_relevant_doc_ids=["doc-a"],
    )
    assert result.score == 0.5
    assert result.evidence["overlap_doc_ids"] == ["doc-a"]


def test_zero_precision_when_retrieved_but_none_relevant() -> None:
    result = heuristic_context_precision(
        retrieved_doc_ids=["doc-x", "doc-y"],
        expected_relevant_doc_ids=["doc-a"],
    )
    assert result.score == 0.0
    assert result.evidence["retrieved_count"] == 2
    assert result.evidence["relevant_retrieved_count"] == 0
    # Distinguishable from the "nothing retrieved" case:
    assert result.evidence["no_documents_retrieved"] is False


def test_zero_documents_retrieved_is_defined_as_score_zero_not_undefined() -> None:
    result = heuristic_context_precision(
        retrieved_doc_ids=[],
        expected_relevant_doc_ids=["doc-a", "doc-b"],
    )
    assert result.score == 0.0
    assert result.evidence["no_documents_retrieved"] is True
    assert result.evidence["retrieved_count"] == 0


def test_zero_documents_retrieved_with_no_expected_docs_still_scores_zero() -> None:
    # Even when the "correct" answer is also to retrieve nothing (an
    # out-of-scope question with no expected relevant docs), this metric
    # cannot reward that -- it can only measure precision over what was
    # retrieved, and nothing was. This is a known limitation, not a bug.
    result = heuristic_context_precision(retrieved_doc_ids=[], expected_relevant_doc_ids=[])
    assert result.score == 0.0
    assert result.evidence["no_documents_retrieved"] is True


def test_score_is_order_independent() -> None:
    a = heuristic_context_precision(["doc-a", "doc-b", "doc-c"], ["doc-c", "doc-a"])
    b = heuristic_context_precision(["doc-c", "doc-b", "doc-a"], ["doc-a", "doc-c"])
    assert a.score == b.score == (2 / 3)


def test_duplicate_retrieved_ids_are_not_double_counted() -> None:
    # A set-based overlap: retrieving the same doc twice should not inflate
    # the numerator beyond the number of distinct relevant docs retrieved,
    # nor should it change the denominator's meaning.
    result = heuristic_context_precision(
        retrieved_doc_ids=["doc-a", "doc-a", "doc-b"],
        expected_relevant_doc_ids=["doc-a"],
    )
    # retrieved_count uses len(retrieved) (3, including the duplicate) by
    # design -- the formula is defined over what the retriever actually
    # returned, duplicates included, not the deduplicated set.
    assert result.evidence["retrieved_count"] == 3
    assert result.score == 1 / 3
