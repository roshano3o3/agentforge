"""A deliberately simple, deterministic retriever.

No embeddings, no ML model, no external API call — this is a keyword
overlap scorer over the synthetic document set in `documents.py`. It exists
so the example RAG app runs fully offline with the local-deterministic
provider, and so retrieval behavior is 100% reproducible run to run.
"""

from __future__ import annotations

import re

from rag_app.documents import DOCUMENTS

_STOPWORDS = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "i",
    "my",
    "me",
    "we",
    "our",
    "you",
    "your",
    "to",
    "for",
    "of",
    "on",
    "in",
    "at",
    "by",
    "with",
    "from",
    "and",
    "or",
    "if",
    "no",
    "not",
    "it",
    "its",
    "can",
    "could",
    "will",
    "would",
    "should",
    "have",
    "has",
    "had",
    "what",
    "when",
    "how",
    "there",
    "any",
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    return {t for t in tokens if t not in _STOPWORDS}


_DOC_TOKENS: dict[str, set[str]] = {doc.id: _tokenize(f"{doc.title} {doc.text}") for doc in DOCUMENTS}


def retrieve_top_k(query: str, k: int = 2, min_score: int = 1) -> list[str]:
    """Return up to `k` document IDs, ranked by keyword-overlap score.

    Score for a document = number of (stopword-filtered) query tokens that
    also appear in the document's title+text. Documents scoring below
    `min_score` (default 1) are excluded entirely -- we never return a
    document that shares no vocabulary with the query. Ties are broken by document ID so the
    ranking is fully deterministic.
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return []

    scored = []
    for doc_id, doc_tokens in _DOC_TOKENS.items():
        score = len(query_tokens & doc_tokens)
        if score >= max(min_score, 1):
            scored.append((doc_id, score))

    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return [doc_id for doc_id, _score in scored[:k]]
