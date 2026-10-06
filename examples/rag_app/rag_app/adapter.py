"""The AgentForge adapter for the example RAG app.

Implements the `agentforge_sdk.Adapter` protocol: given a test case's input
text, retrieve relevant documents and produce an answer. This is trusted
local code executed in-process by the AgentForge worker -- no sandboxing.

No model is called. The reported "tokens" are whitespace-separated word
counts of the prompt (question + retrieved text) and the answer, and the
reported model is "local-deterministic" -- honest measurements of this
fixture, not of any real LLM.

Replay overrides (agentforge_sdk.replay): `top_k`, or a `retrieval_config`
object with `top_k` and `min_score` (the minimum keyword overlap a document
needs). There's no prompt and no model to replace, so neither is declared.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agentforge_sdk import AdapterOutput, tracing
from agentforge_sdk.replay import OverrideError, Setting, replayable
from rag_app.documents import DOCUMENTS_BY_ID
from rag_app.retriever import retrieve_top_k

TOP_K = 2
MODEL_NAME = "local-deterministic"


def _word_count(text: str) -> int:
    return len(text.split())


def _one_top_k(values: dict[str, Any]) -> None:
    if "top_k" in values and "top_k" in values.get("retrieval_config", {}):
        raise OverrideError("top_k is set both directly and in retrieval_config; set it once")


_TOP_K = Setting("top_k", "int", "Documents to retrieve", minimum=1, maximum=10)


@replayable(
    _TOP_K,
    Setting(
        "retrieval_config",
        "object",
        "Retrieval settings",
        fields=(_TOP_K, Setting("min_score", "int", "Minimum keyword overlap for a document", minimum=1, maximum=20)),
    ),
    check=_one_top_k,
)
def answer(input_text: str, overrides: Mapping[str, Any] | None = None) -> AdapterOutput:
    config = {"top_k": TOP_K, "min_score": 1, **(overrides or {}).get("retrieval_config", {})}
    if overrides and "top_k" in overrides:
        config["top_k"] = overrides["top_k"]
    k, min_score = config["top_k"], config["min_score"]
    # A `retrieval` span (no-op without OpenTelemetry): what was asked and what came back.
    with tracing.span(
        "retrieval",
        {"agentforge.component": "agent", "agentforge.retrieval.query": input_text, "agentforge.retrieval.k": k},
    ) as span:
        retrieved_doc_ids = retrieve_top_k(input_text, k=k, min_score=min_score)
        span.set("agentforge.retrieval.doc_ids", retrieved_doc_ids)
        span.set("agentforge.retrieval.documents", [DOCUMENTS_BY_ID[d].text for d in retrieved_doc_ids])

    if not retrieved_doc_ids:
        text = (
            "I could not find relevant policy information for this question in the "
            "Northwind Outfitters policy documents I have access to."
        )
        return AdapterOutput(
            answer=text,
            retrieved_doc_ids=[],
            citations=[],
            input_tokens=_word_count(input_text),
            output_tokens=_word_count(text),
            model=MODEL_NAME,
        )

    top_doc = DOCUMENTS_BY_ID[retrieved_doc_ids[0]]
    snippet = top_doc.text.split(". ")[0].strip().rstrip(".") + "."
    text = f"Based on {top_doc.id} ({top_doc.title}): {snippet}"
    prompt = " ".join([input_text, *(DOCUMENTS_BY_ID[d].text for d in retrieved_doc_ids)])
    return AdapterOutput(
        answer=text,
        retrieved_doc_ids=retrieved_doc_ids,
        citations=[top_doc.id],
        input_tokens=_word_count(prompt),
        output_tokens=_word_count(text),
        model=MODEL_NAME,
    )
