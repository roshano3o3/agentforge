"""Run lifecycle: API creates + enqueues, the worker's real `execute_run`
executes (called in-process here instead of via Redis), results persist.

Exercises the real adapters (the example RAG app, its fault-injection
variant, and the HTTP contract against a real local HTTP server) and every
registered evaluator. The end-to-end path through real Redis and the Docker
worker is covered by tests/e2e.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_api.models.evaluation import EvaluationRun, RunStatus
from agentforge_api.services import runs as run_service
from agentforge_evaluators import DEFAULT_EVALUATORS, ModelPrice
from agentforge_worker.runner import execute_run
from rag_app.http_server import make_server

pytestmark = pytest.mark.asyncio

PRICING = {"local-deterministic": ModelPrice(0.0, 0.0)}

RAG_CASES = [
    {
        "case_key": "refund",
        "input": "Can I return hiking boots after 30 days without tags?",
        "expected_answer_contains": ["45 days"],
        "expected_context": ["policy-returns-001"],
    },
    {
        "case_key": "warranty",
        "input": "Do you offer a warranty on outerwear jackets?",
        "expected_answer_regex": r"\b2 year\b",
        "expected_context": ["policy-warranty-003"],
    },
]

FAULT_CASES = [
    {"case_key": "a-ok", "input": "Do you offer a warranty on outerwear jackets?", "expected_context": ["policy-warranty-003"]},
    {"case_key": "b-timeout", "input": "[[sleep:3]] warranty on outerwear?", "expected_context": []},
    {"case_key": "c-crash", "input": "[[crash]] returns?", "expected_context": []},
    {"case_key": "d-exit", "input": "[[exit]] returns?", "expected_context": []},
    {"case_key": "e-bad-output", "input": "[[bad-output]] returns?", "expected_context": []},
]


async def _setup(client: AsyncClient, cases: list[dict], *, publish: bool = True) -> dict:
    application = (await client.post("/applications", json={"name": "app"})).json()
    app_version = (await client.post(f"/applications/{application['id']}/versions", json={"version": "v1"})).json()
    dataset = (await client.post("/datasets", json={"name": "ds"})).json()
    draft = (await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": cases})).json()
    if publish:
        resp = await client.post(f"/datasets/ds/versions/{draft['version']}/publish")
        assert resp.status_code == 200, resp.text
    return {
        "application_id": application["id"],
        "application_version_id": app_version["id"],
        "dataset_version_id": draft["id"],
    }


def _payload(ids: dict, target: str = "rag_app.adapter:answer", adapter_type: str = "python", **extra) -> dict:
    return {**ids, "adapter": {"type": adapter_type, "target": target}, **extra}


async def _create(client: AsyncClient, payload: dict) -> dict:
    resp = await client.post("/runs", json=payload)
    assert resp.status_code == 202, resp.text
    return resp.json()


async def test_post_run_creates_pending_run_and_enqueues_it(client: AsyncClient, queue) -> None:
    ids = await _setup(client, RAG_CASES)
    run = await _create(client, _payload(ids))

    assert run["status"] == "pending"
    assert queue.enqueued == [run["id"]]
    assert run["evaluators"] == DEFAULT_EVALUATORS  # pinned name@version, all registered
    assert run["labels"] == ["fixture-based"]
    assert run["progress"] == {"completed_cases": 0, "total_cases": 2}
    assert run["aggregates"] is None
    assert run["results"] == []


async def test_run_against_draft_dataset_version_is_rejected(client: AsyncClient, queue) -> None:
    ids = await _setup(client, RAG_CASES, publish=False)
    resp = await client.post("/runs", json=_payload(ids))
    assert resp.status_code == 400
    assert "draft" in resp.json()["detail"].lower()
    assert queue.enqueued == []


async def test_unknown_evaluator_is_rejected_before_anything_is_persisted(client: AsyncClient, queue) -> None:
    ids = await _setup(client, RAG_CASES)
    resp = await client.post("/runs", json=_payload(ids, evaluators=["exact_match", "made_up_metric"]))
    assert resp.status_code == 400
    assert "made_up_metric" in resp.json()["detail"]
    assert (await client.get("/runs")).json() == []


@pytest.mark.parametrize(
    ("adapter_type", "target"),
    [("python", "no_colon_here"), ("python", "mod:fn; import os"), ("http", "ftp://example.com")],
)
async def test_malformed_adapter_spec_is_rejected(client: AsyncClient, adapter_type: str, target: str) -> None:
    ids = await _setup(client, RAG_CASES)
    resp = await client.post("/runs", json=_payload(ids, target=target, adapter_type=adapter_type))
    assert resp.status_code == 422


async def test_unreachable_queue_marks_run_failed_instead_of_leaving_it_pending(client: AsyncClient, queue) -> None:
    ids = await _setup(client, RAG_CASES)
    queue.fail_with = "connection refused"
    resp = await client.post("/runs", json=_payload(ids))
    assert resp.status_code == 503

    [run] = (await client.get("/runs")).json()
    assert run["status"] == "failed"
    assert "connection refused" in run["error_message"]


async def test_worker_executes_run_to_completion_with_every_evaluator(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client, RAG_CASES)
    run = await _create(client, _payload(ids, threshold=0.5))

    assert await execute_run(session_factory, run["id"], pricing=PRICING) == "completed"
    done = (await client.get(f"/runs/{run['id']}")).json()

    assert done["status"] == "completed"
    assert done["started_at"] is not None and done["completed_at"] is not None
    assert done["progress"] == {"completed_cases": 2, "total_cases": 2}
    for result in done["results"]:
        assert result["status"] == "ok"
        keys = {f"{m['evaluator_name']}@{m['evaluator_version']}" for m in result["metrics"]}
        assert keys == set(DEFAULT_EVALUATORS)
        for m in result["metrics"]:
            assert "fixture-based" in m["labels"]
            assert m["reason"]
        cost = next(m for m in result["metrics"] if m["evaluator_name"] == "estimated_cost")
        assert "estimated" in cost["labels"]
        assert cost["value"] == 0.0 and cost["unit"] == "usd"
        assert result["model"] == "local-deterministic"
        assert result["citations"] == result["retrieved_doc_ids"][:1]

    by_case = {r["case_key"]: r for r in done["results"]}
    metrics = {m["evaluator_name"]: m for m in by_case["warranty"]["metrics"]}
    assert metrics["answer_regex"]["passed"] is True  # answer says "2 year manufacturer warranty"
    assert metrics["exact_match"]["passed"] is None  # no expected_answer -> not applicable

    agg = done["aggregates"]
    assert agg["case_count"] == 2
    assert agg["passed_count"] == sum(1 for r in done["results"] if r["passed"])
    assert agg["latency_ms"]["p50"] is not None and agg["latency_ms"]["p95"] is not None
    assert agg["estimated_cost_usd"]["label"] == "estimated"
    assert set(agg["metrics"]) == set(DEFAULT_EVALUATORS)

    # Re-fetching returns identical persisted data.
    assert (await client.get(f"/runs/{run['id']}")).json() == done
    listed = (await client.get("/runs")).json()
    assert listed[0]["id"] == run["id"] and listed[0]["aggregates"] == agg


async def test_timeout_and_crashing_cases_are_recorded_and_the_run_still_completes(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client, FAULT_CASES)
    run = await _create(client, _payload(ids, target="rag_app.fault_injection:answer", timeout_seconds=0.5))

    assert await execute_run(session_factory, run["id"], pricing=PRICING) == "completed"
    done = (await client.get(f"/runs/{run['id']}")).json()
    assert done["status"] == "completed"
    by_case = {r["case_key"]: r for r in done["results"]}
    assert len(by_case) == 5

    assert by_case["a-ok"]["status"] == "ok"
    assert by_case["a-ok"]["metrics"]

    timeout = by_case["b-timeout"]
    assert timeout["status"] == "timeout"
    assert "0.5s" in timeout["error_message"]
    assert 400 <= timeout["latency_ms"] < 3000  # gave up at the timeout, didn't wait out the 3s sleep

    crash = by_case["c-crash"]
    assert (crash["status"], crash["error_type"]) == ("error", "RuntimeError")
    assert "crashed on purpose" in crash["error_message"]
    assert "Traceback" in crash["error_message"]

    exit_case = by_case["d-exit"]  # SystemExit is a BaseException: still contained
    assert (exit_case["status"], exit_case["error_type"]) == ("error", "SystemExit")

    bad = by_case["e-bad-output"]
    assert bad["status"] == "error"
    assert "expected AdapterOutput" in bad["error_message"]

    for key in ("b-timeout", "c-crash", "d-exit", "e-bad-output"):
        assert by_case[key]["passed"] is False
        assert by_case[key]["metrics"] == []

    agg = done["aggregates"]
    assert (agg["ok_count"], agg["timeout_count"], agg["error_count"]) == (1, 1, 3)
    assert agg["pass_rate"] <= 0.2


async def test_finished_run_is_immutable_in_the_service_layer(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client, RAG_CASES)
    run = await _create(client, _payload(ids))
    await execute_run(session_factory, run["id"], pricing=PRICING)
    before = (await client.get(f"/runs/{run['id']}")).json()

    # Re-delivering the job is a no-op, not a re-execution.
    assert await execute_run(session_factory, run["id"], pricing=PRICING) == "skipped"

    async with session_factory() as session:
        row = await session.get(EvaluationRun, run["id"])
        for target in (RunStatus.running, RunStatus.failed, RunStatus.pending):
            with pytest.raises(run_service.RunStateError):
                run_service.transition(row, target)
        with pytest.raises(run_service.RunStateError):
            run_service.assert_accepts_results(row)

    assert (await client.get(f"/runs/{run['id']}")).json() == before
    # And there's no route for a client to write results at all.
    assert (await client.post(f"/runs/{run['id']}/results", json={"results": []})).status_code in (404, 405)


async def test_run_left_running_by_a_dead_worker_restarts_from_scratch(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client, RAG_CASES)
    run = await _create(client, _payload(ids))
    await execute_run(session_factory, run["id"], pricing=PRICING)  # produce a real result set...

    # ...then simulate "a previous attempt died mid-run": a second run left
    # `running` with one partial result copied in.
    second = await _create(client, _payload(ids))
    async with session_factory() as session:
        row = await session.get(EvaluationRun, second["id"])
        run_service.transition(row, RunStatus.running)
        await session.commit()
    first_case = (await client.get(f"/runs/{run['id']}")).json()["results"][0]
    from agentforge_api.models.evaluation import EvaluationResult, ResultStatus

    async with session_factory() as session:
        session.add(
            EvaluationResult(
                evaluation_run_id=second["id"], test_case_id=first_case["test_case_id"],
                latency_ms=1.0, status=ResultStatus.ok, passed=True,
            )
        )
        await session.commit()

    assert await execute_run(session_factory, second["id"], pricing=PRICING) == "completed"
    done = (await client.get(f"/runs/{second['id']}")).json()
    assert done["progress"] == {"completed_cases": 2, "total_cases": 2}
    assert all(r["metrics"] for r in done["results"])  # the partial row was replaced, not kept


async def test_unimportable_adapter_fails_the_run_with_a_reason(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client, RAG_CASES)
    run = await _create(client, _payload(ids, target="does_not_exist.module:answer"))

    assert await execute_run(session_factory, run["id"], pricing=PRICING) == "failed"
    done = (await client.get(f"/runs/{run['id']}")).json()
    assert done["status"] == "failed"
    assert "does_not_exist.module" in done["error_message"]
    assert done["completed_at"] is not None


@pytest.fixture
def http_adapter_url() -> Iterator[str]:
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


async def test_http_adapter_contract(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession], http_adapter_url: str
) -> None:
    ids = await _setup(client, RAG_CASES)
    ok_run = await _create(client, _payload(ids, adapter_type="http", target=f"{http_adapter_url}/answer"))
    bad_run = await _create(client, _payload(ids, adapter_type="http", target=f"{http_adapter_url}/missing"))

    assert await execute_run(session_factory, ok_run["id"], pricing=PRICING) == "completed"
    assert await execute_run(session_factory, bad_run["id"], pricing=PRICING) == "completed"

    ok = (await client.get(f"/runs/{ok_run['id']}")).json()
    assert [r["status"] for r in ok["results"]] == ["ok", "ok"]
    assert all(r["retrieved_doc_ids"] and r["model"] == "local-deterministic" for r in ok["results"])

    bad = (await client.get(f"/runs/{bad_run['id']}")).json()
    assert [r["status"] for r in bad["results"]] == ["error", "error"]
    assert "404" in bad["results"][0]["error_message"]
