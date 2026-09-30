"""The contract an application implements so AgentForge can evaluate it.

Two adapter kinds, both executed by the AgentForge worker (inside Docker):

* **python** -- `"module.path:function"`: a callable (sync or async) taking a
  test case's input text and returning an `AdapterOutput` (or a dict of the
  same fields, or a plain answer string). It is imported and called
  in-process by the worker, so it is **trusted local code**: no sandbox, no
  isolation -- it can do anything the worker process can. The module must be
  installed in the worker image.

* **http** -- a URL. The worker POSTs `{"input": "...", "case_key": "..."}`
  as JSON and expects a 2xx JSON response with `AdapterOutput`'s fields:

      {"answer": "...",                      # required
       "retrieved_doc_ids": ["..."],         # optional, default []
       "citations": ["..."],                 # optional, default []
       "input_tokens": 123, "output_tokens": 45,  # optional
       "model": "..."}                       # optional

  The endpoint is also trusted: the worker calls whatever URL the run names.
  From inside Docker, a service on your host is `http://host.docker.internal:<port>`.

Both kinds get the same per-case timeout and exception capture: a case that
raises, times out, or returns a malformed output is recorded as an error
result and the run continues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class AdapterOutput:
    """What an application adapter returns for one test-case input.

    Token counts and model name are optional and only ever *reported* by the
    adapter -- AgentForge never guesses them. Leave them None if unknown.
    """

    answer: str
    retrieved_doc_ids: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


class Adapter(Protocol):
    """Callable an example/target application implements."""

    def __call__(self, input_text: str) -> AdapterOutput: ...
