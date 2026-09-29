"""The contract an application implements so AgentForge can evaluate it.

Phase 1 only supports in-process adapters: a plain Python callable living in
the same process as the CLI. This is treated as trusted local code — the
caller runs it directly, with no sandboxing and no guarantee it can't do
anything the calling process could do. An out-of-process HTTP adapter
variant (for evaluating an application AgentForge doesn't share a process
with) is future work, not implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class AdapterOutput:
    """What an application adapter returns for one test-case input."""

    answer: str
    retrieved_doc_ids: list[str] = field(default_factory=list)


class Adapter(Protocol):
    """Callable an example/target application implements.

    `input_text` is a test case's `input` field; the adapter returns the
    application's answer plus whichever document IDs it retrieved (if any)
    to produce that answer.
    """

    def __call__(self, input_text: str) -> AdapterOutput: ...
