"""The AgentForge adapter for the example RAG app.

Implements the `agentforge_sdk.Adapter` protocol: given a test case's input
text, retrieve relevant documents and produce an answer. This is trusted
local code executed in-process by the CLI -- no sandboxing.
"""

from __future__ import annotations

from agentforge_sdk import AdapterOutput
from rag_app.documents import DOCUMENTS_BY_ID
from rag_app.retriever import retrieve_top_k

TOP_K = 2


def answer(input_text: str) -> AdapterOutput:
    retrieved_doc_ids = retrieve_top_k(input_text, k=TOP_K)

    if not retrieved_doc_ids:
        return AdapterOutput(
            answer=(
                "I could not find relevant policy information for this question in the "
                "Northwind Outfitters policy documents I have access to."
            ),
            retrieved_doc_ids=[],
        )

    top_doc = DOCUMENTS_BY_ID[retrieved_doc_ids[0]]
    snippet = top_doc.text.split(". ")[0].strip().rstrip(".") + "."
    return AdapterOutput(
        answer=f"Based on {top_doc.id} ({top_doc.title}): {snippet}",
        retrieved_doc_ids=retrieved_doc_ids,
    )
