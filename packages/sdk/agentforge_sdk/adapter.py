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
       "model": "...",                       # optional
       "steps": [                            # optional: the agent's trajectory, in order
         {"kind": "tool_call", "name": "get_invoice",
          "args": {"invoice_id": "INV-1002"}, "result": {...}, "error": null},
         {"kind": "retrieval", "name": "retriever", "retrieved_doc_ids": ["doc-1"]},
         {"kind": "final_answer", "output": "..."}
       ]}

  The endpoint is also trusted: the worker calls whatever URL the run names.
  From inside Docker, a service on your host is `http://host.docker.internal:<port>`.

Both kinds get the same per-case timeout and exception capture: a case that
raises, times out, or returns a malformed output is recorded as an error
result and the run continues.

**Scenarios** (adversarial cases): a case may carry a `scenario` describing
its environment -- mock tool failures, text merged into a tool's result, the
session user's permissions (format: agentforge_core.scenario). A python
adapter receives it as a `scenario` keyword argument *if it declares one*
(`def answer(input_text, scenario=None)`); a case with a scenario sent to an
adapter that doesn't declare it is recorded as an error, never run without
its setup. An http adapter gets it as a `"scenario"` field in the request
body, only when the case has one. It describes the environment only: the
case's attack category and expectations are never sent.

This module is deliberately plain dataclasses: no server, DB, or framework
imports, so any application can depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StepKind = Literal["retrieval", "tool_call", "final_answer"]


@dataclass
class Step:
    """One step of an agent's trajectory, as the agent reports it.

    * ``tool_call``: ``name`` is the tool, ``args`` its arguments (a JSON
      object), and exactly what came back -- ``result`` (any JSON value) or
      ``error`` (a message) if the tool failed.
    * ``retrieval``: ``name`` is the retriever, ``retrieved_doc_ids`` what it returned.
    * ``final_answer``: ``output`` is the answer text.

    ``duration_ms`` is optional and, like everything here, only ever reported
    by the agent -- AgentForge doesn't invent timings. So is ``span_id``: the
    16-hex-digit id of the span the agent opened for this step (see
    agentforge_sdk.tracing), which links the stored step to its span.
    """

    kind: StepKind
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    retrieved_doc_ids: list[str] = field(default_factory=list)
    output: str | None = None
    duration_ms: float | None = None
    span_id: str | None = None


@dataclass
class AdapterOutput:
    """What an application adapter returns for one test-case input.

    Token counts and model name are optional and only ever *reported* by the
    adapter -- AgentForge never guesses them. Leave them None if unknown.
    ``steps`` is the ordered trajectory for agents; plain RAG apps can leave
    it empty.
    """

    answer: str
    retrieved_doc_ids: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    steps: list[Step] = field(default_factory=list)


class Adapter(Protocol):
    """Callable an example/target application implements. Declaring a
    `scenario` parameter is optional; only adversarial cases need it."""

    def __call__(self, input_text: str) -> AdapterOutput: ...
