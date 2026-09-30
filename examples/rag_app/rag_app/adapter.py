"""The AgentForge adapter for the example RAG app.

Implements the `agentforge_sdk.Adapter` protocol: given a test case's input
text, retrieve relevant documents and produce an answer. This is trusted
local code executed in-process by the AgentForge worker -- no sandboxing.

No model is called. The reported "tokens" are whitespace-separated word
counts of the prompt (question + retrieved text) and the answer, and the
reported model is "local-deterministic" -- honest measurements of this
fixture, not of any real LLM.
"""

from __future__ import annotations

from agentforge_sdk import AdapterOutput
from rag_app.documents import DOCUMENTS_BY_ID
from rag_app.retriever import retrieve_top_k

TOP_K = 2
MODEL_NAME = "local-deterministic"


def _word_count(text: str) -> int:
    return len(text.split())


def answer(input_text: str) -> AdapterOutput:
    retrieved_doc_ids = retrieve_top_k(input_text, k=TOP_K)

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
