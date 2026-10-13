"""Tools in-process vs the same tools over MCP: the evaluation must not be able to tell the difference.

Each agent below runs every case of the trajectory dataset (10) and the safety dataset (35) twice through the
real API and the worker's `execute_run`: once with its tools in-process, once with `tools=mcp`, which launches
`python -m invoice_agent.mcp_server` as a stdio subprocess and calls every tool over the MCP protocol (the
official SDK on both sides). Per case, these must be identical:

- **Result:** status, case verdict and answer.
- **Trajectory:** every step's kind, tool, args, result or error, and output.
- **Evaluators:** every evaluator's version, score, value, verdict, reason and evidence.
- **Stored span tree:** names, nesting, attributes, status and events.

Only what is measured rather than produced is ignored (timings, ids, the latency evaluator's measured value and
reason), plus one thing that is genuinely different and says so: an error event's `exception.stacktrace`, which
over MCP shows the client re-raising the server's error. Its `exception.type` and `exception.message` match.

Agents covered: the scripted planner as v1, v2 and v1 without D2 (demo PR #4), and the LLM planner driven by a
fake provider (tests/llm_fakes.py), which must also be offered identical tool schemas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from llm_fakes import PlannerBackedModel, use
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_worker.runner import execute_run

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[2]
DATASETS = ("invoice_agent_v1.yaml", "invoice_agent_safety_v1.yaml")
HAIKU = {"provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.0}
# Measured, not produced: differs between any two runs of the same code.
SPAN_KEYS_IGNORED = {"span_id", "parent_span_id", "start_time", "end_time", "duration_ms"}
MEASURED_ATTRIBUTES = ("latency", "duration", "_ms", ".id")
KEPT_ID_ATTRIBUTES = {"gen_ai.tool.call.id"}  # the planner's own call ids: produced, so they must match


@pytest_asyncio.fixture
async def submit(client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]):
    app = (await client.post("/applications", json={"name": "mcp-parity"})).json()
    version = (await client.post(f"/applications/{app['id']}/versions", json={"version": "v"})).json()
    published: dict[str, str] = {}

    async def go(file: str, target: str, settings: dict[str, Any] | None) -> dict:
        if file not in published:
            name, _d, cases, defaults = validate_dataset_file(REPO / "datasets" / file)
            ds = (await client.post("/datasets", json={"name": name})).json()
            v = (
                await client.post(
                    f"/datasets/{ds['id']}/versions",
                    json={"test_cases": [c.model_dump() for c in cases], "default_evaluators": defaults},
                )
            ).json()
            await client.post(f"/datasets/{name}/versions/{v['version']}/publish")
            published[file] = v["id"]
        resp = await client.post(
            "/runs",
            json={
                "application_id": app["id"],
                "application_version_id": version["id"],
                "dataset_version_id": published[file],
                "adapter": {"type": "python", "target": target},
                "provider_type": "llm:anthropic" if target.endswith("answer_llm") else "local-deterministic",
                "timeout_seconds": 30,
                "adapter_settings": settings,
            },
        )
        assert resp.status_code == 202, resp.text
        run_id = resp.json()["id"]
        status = await execute_run(session_factory, run_id, trace_context=queue.trace_contexts[run_id])
        run = (await client.get(f"/runs/{run_id}")).json()
        assert status == "completed", run["error_message"]
        return run

    return go


def _attributes(attrs: dict[str, Any], measured: bool = False) -> dict[str, Any]:
    """Without ids and timings; for the latency evaluator, also without its measured value, reason, evidence."""
    return {
        k: v
        for k, v in attrs.items()
        if (k in KEPT_ID_ATTRIBUTES or not any(marker in k for marker in MEASURED_ATTRIBUTES))
        and not (measured and k.rsplit(".", 1)[-1] in {"reason", "value", "evidence"})
    }


def _span_tree(span: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": span["name"],
        "kind": span["kind"],
        "service": span["service"],
        "attributes": _attributes(span["attributes"], measured=span["name"] == "evaluate latency"),
        "status": (span["status_code"], span["status_message"]),
        # exception.type and exception.message must match; exception.stacktrace can't: it's where this process
        # raised the error, which over MCP is the client re-raising the server's error (mcp_client.py), not the
        # tool's own frames. It's the one stored difference, and it describes code location, not the evaluation.
        "events": [
            {
                "name": e.get("name"),
                "attributes": {k: v for k, v in (e.get("attributes") or {}).items() if k != "exception.stacktrace"},
            }
            for e in span["events"]
        ],
        "step_index": span["step_index"],
        # Siblings as a multiset: the API orders them by start time, which ties on a coarse (Windows) clock.
        # Each sibling stays identifiable by its own attributes (planner turn, tool, evaluator, step).
        "children": sorted((_span_tree(c) for c in span["children"]), key=repr),
    }


def _first_difference(a: Any, b: Any, path: str = "") -> str:
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                return _first_difference(a.get(k), b.get(k), f"{path}.{k}")
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            if x != y:
                return _first_difference(x, y, f"{path}[{i}]")
    return f"{path}: {a!r} != {b!r}"


def _case_span(roots: list[dict[str, Any]]) -> dict[str, Any]:
    """The case's own subtree (the run-level spans above it describe the run, which differs by design)."""
    stack = list(roots)
    while stack:
        span = stack.pop()
        if span["name"] == "agentforge.case":
            return span
        stack.extend(span["children"])
    raise AssertionError("no agentforge.case span")


def _metric(m: dict[str, Any]) -> dict[str, Any]:
    out = {k: m[k] for k in ("evaluator_name", "evaluator_version", "score", "passed", "evidence", "reason", "value")}
    if m["evaluator_name"] == "latency":
        out.pop("value"), out.pop("reason"), out.pop("evidence")
    return out


def _step(s: dict[str, Any]) -> tuple[Any, ...]:
    return (s["step_index"], s["kind"], s["name"], s["args"], s["result"], s["error"], s["output"])


async def _assert_identical(client: AsyncClient, local: dict, remote: dict) -> int:
    assert local["aggregates"]["pass_rate"] == remote["aggregates"]["pass_rate"]
    by_key = {r["case_key"]: r for r in remote["results"]}
    assert set(by_key) == {r["case_key"] for r in local["results"]}
    for a in local["results"]:
        b = by_key[a["case_key"]]
        key = a["case_key"]
        assert (a["status"], a["passed"], a["output_answer"]) == (b["status"], b["passed"], b["output_answer"]), key
        assert (a["input_tokens"], a["output_tokens"], a["model"]) == (
            b["input_tokens"],
            b["output_tokens"],
            b["model"],
        )
        assert [_step(s) for s in a["steps"]] == [_step(s) for s in b["steps"]], key
        assert sorted(map(str, map(_metric, a["metrics"]))) == sorted(map(str, map(_metric, b["metrics"]))), key
        ta = (await client.get(f"/traces/{a['id']}")).json()
        tb = (await client.get(f"/traces/{b['id']}")).json()
        assert ta["span_count"] == tb["span_count"], key
        sa, sb = _span_tree(_case_span(ta["spans"])), _span_tree(_case_span(tb["spans"]))
        assert sa == sb, f"{key}: {_first_difference(sa, sb)}"
    return len(local["results"])


@pytest.mark.parametrize("target", ["answer_v1", "answer_v2", "answer_v1_without_d2"])
async def test_scripted_agent_is_identical_over_mcp(submit, client: AsyncClient, target: str) -> None:
    compared = 0
    for file in DATASETS:
        local = await submit(file, f"invoice_agent.adapter:{target}", None)
        remote = await submit(file, f"invoice_agent.adapter:{target}", {"tools": "mcp"})
        assert remote["adapter_settings"] == {"tools": "mcp"}
        compared += await _assert_identical(client, local, remote)
    assert compared == 45


class RecordingModel(PlannerBackedModel):
    """The fake provider, also keeping the tool schemas it was offered on every turn."""

    def __init__(self) -> None:
        super().__init__(input_tokens=100, output_tokens=20)
        self.offered: list[list[dict[str, Any]]] = []

    def complete(self, config, system, messages, tools):  # type: ignore[no-untyped-def]
        self.offered.append(list(tools))
        return super().complete(config, system, messages, tools)


async def test_llm_planner_is_identical_over_mcp(monkeypatch, submit, client: AsyncClient) -> None:
    compared = 0
    for file in DATASETS:
        local_model = use(monkeypatch, RecordingModel())
        local = await submit(file, "invoice_agent.adapter:answer_llm", HAIKU)
        remote_model = use(monkeypatch, RecordingModel())
        remote = await submit(file, "invoice_agent.adapter:answer_llm", HAIKU | {"tools": "mcp"})
        # The model is offered the same tool definitions (the MCP server publishes the in-process schemas).
        assert local_model.offered == remote_model.offered and local_model.offered
        compared += await _assert_identical(client, local, remote)
    assert compared == 45


async def test_an_unreachable_mcp_server_fails_the_case_with_its_reason(submit) -> None:
    run = await submit(
        "invoice_agent_v1.yaml",
        "invoice_agent.adapter:answer_v1",
        {"tools": "mcp", "mcp_server": "http://127.0.0.1:9/mcp"},  # nothing listens on port 9 (discard)
    )
    assert {r["status"] for r in run["results"]} == {"error"}
    assert all("MCP server http://127.0.0.1:9/mcp" in (r["error_message"] or "") for r in run["results"])
