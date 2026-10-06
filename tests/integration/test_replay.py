"""Failure replay through the real API and the worker's real execute_replay.

* PR #4's failing indirect-injection case (v1 with D2 off), replayed with D2
  back on: it passes injection_resisted and the delete_invoice call is gone.
* Determinism: a replay without overrides reproduces the original exactly --
  every case of the trajectory and safety datasets under three agents, the
  RAG app, and the fault-injection app's error results.
* Overrides the adapter didn't declare are rejected with a message, never
  ignored; the original run is never modified; finished replays are frozen.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_api.models.evaluation import EvaluationResult, EvaluationRun, RunStatus
from agentforge_api.models.replay import Replay, ReplaySpan, ReplayStep
from agentforge_api.services import replays as replay_service
from agentforge_api.services.runs import transition
from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_worker.replay import execute_replay
from agentforge_worker.runner import execute_run

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[2]
PR4_CASE = "injection_indirect.memo-delete@get_invoice.contact-lookup-001"
POSTGRES = "AGENTFORGE_TEST_DATABASE_URL"


@pytest_asyncio.fixture
async def run(client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]):
    """run(dataset file, adapter target) -> the completed run's JSON."""
    app = (await client.post("/applications", json={"name": "replay-test"})).json()
    version = (await client.post(f"/applications/{app['id']}/versions", json={"version": "v"})).json()
    published: dict[str, str] = {}

    async def go(file: str, target: str, adapter_type: str = "python") -> dict:
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
                "adapter": {"type": adapter_type, "target": target},
                "timeout_seconds": 2,
            },
        )
        assert resp.status_code == 202, resp.text
        run_id = resp.json()["id"]
        assert await execute_run(session_factory, run_id, trace_context=queue.trace_contexts[run_id]) == "completed"
        return (await client.get(f"/runs/{run_id}")).json()

    return go


@pytest_asyncio.fixture
async def replay(client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]):
    """replay(result id, overrides) -> the finished replay's JSON (POST /replay, then the worker)."""

    async def go(result_id: str, overrides: dict[str, Any] | None = None) -> dict:
        resp = await client.post("/replay", json={"result_id": result_id, "overrides": overrides or {}})
        assert resp.status_code == 202, resp.text
        created = resp.json()
        assert created["status"] == "pending"
        assert queue.enqueued_replays[-1] == created["id"]
        await execute_replay(session_factory, created["id"], trace_context=queue.trace_contexts[created["id"]])
        return (await client.get(f"/replays/{created['id']}")).json()

    return go


def _case(run: dict, key: str) -> dict:
    return next(r for r in run["results"] if r["case_key"] == key)


def _verdicts(metrics: list[dict]) -> dict[str, Any]:
    return {m["evaluator_name"]: (m["evaluator_version"], m["passed"], m["score"]) for m in metrics}


def _steps(steps: list[dict]) -> list[tuple]:
    return [(s["kind"], s["name"], s["args"], s["result"], s["error"], s["output"]) for s in steps]


async def test_pr4_injection_case_passes_when_replayed_with_d2_on(
    run, replay, client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    pr4 = await run("invoice_agent_safety_v1.yaml", "invoice_agent.adapter:answer_v1_without_d2")
    original = _case(pr4, PR4_CASE)
    assert original["passed"] is False
    assert {m["evaluator_name"] for m in original["metrics"] if m["passed"] is False} == {"injection_resisted"}
    assert "delete_invoice" in [s["name"] for s in original["steps"]]
    before = (await client.get(f"/runs/{pr4['id']}")).json()

    r = await replay(original["id"], {"d2": "on"})

    assert r["status"] == "completed", r["error_message"]
    assert r["overrides"] == {"d2": "on"}
    assert r["passed"] is True and r["original_passed"] is False
    assert r["case_key"] == PR4_CASE and r["original_run_id"] == pr4["id"]
    assert r["dataset_content_hash"] == pr4["dataset_content_hash"]
    assert r["evaluators"] == pr4["evaluators"]  # the original run's pinned versions
    assert r["labels"] == ["fixture-based"]
    injection = next(m for m in r["metrics"] if m["evaluator_name"] == "injection_resisted")
    assert injection["passed"] is True
    assert [s["name"] for s in r["steps"]] == ["get_invoice", "get_customer", ""]
    assert "delete_invoice" not in [s["name"] for s in r["steps"]]

    diff = r["diff"]
    assert diff["identical"] is False
    assert (diff["before_passed"], diff["after_passed"]) == (False, True)
    changes = {e["evaluator_name"]: e["change"] for e in diff["evaluators"]}
    assert changes["injection_resisted"] == "fixed"
    assert {n: c for n, c in changes.items() if c not in ("unchanged",)} == {"injection_resisted": "fixed"}
    ops = [(s["op"], s["name"]) for s in diff["trajectory"]]
    assert ops == [
        ("unchanged", "get_invoice"),
        ("removed", "delete_invoice"),
        ("unchanged", "get_customer"),
        ("unchanged", ""),  # the final answer: same text
    ]
    removed = diff["trajectory"][1]["before"]
    assert removed["step_index"] == 2 and removed["args"] == {"invoice_id": "INV-1004"}
    assert diff["answer"]["changed"] is False
    assert diff["latency_ms"]["before"] == original["latency_ms"] and diff["latency_ms"]["delta"] is not None
    assert diff["input_tokens"] == {"before": None, "after": None, "delta": None}  # the agent reports none
    assert "step 2 delete_invoice: removed" in diff["differences"]

    # Its own spans, redacted: the replay's tree from the API through the agent and evaluators.
    [root] = r["spans"]
    assert root["name"] == "agentforge.replay.create"
    [worker] = root["children"]
    assert worker["name"] == "agentforge.replay"
    assert worker["attributes"]["agentforge.replay.original_result_id"] == original["id"]
    [case] = worker["children"]
    assert case["name"] == "agentforge.case"
    names = json.dumps(r["spans"])
    assert "execute_tool delete_invoice" not in names and "execute_tool get_customer" in names
    assert "ap@kestrel-robotics.example" not in names and "[EMAIL]" in names
    assert "ap@kestrel-robotics.example" in r["output_answer"]  # only spans are redacted
    async with session_factory() as session:
        stored = list(await session.scalars(select(ReplaySpan).where(ReplaySpan.replay_id == r["id"])))
    # create, replay, case; invoke_agent, 3 decisions, 2 tool calls; one per evaluator
    assert len(stored) == 3 + 6 + len(r["metrics"])

    # The original run is untouched, and lists the replay.
    assert (await client.get(f"/runs/{pr4['id']}")).json() == before
    [listed] = (await client.get(f"/results/{original['id']}/replays")).json()
    assert listed["id"] == r["id"] and listed["passed"] is True and listed["identical"] is False


@pytest.mark.parametrize(
    ("file", "targets"),
    [
        ("invoice_agent_v1.yaml", ["invoice_agent.adapter:answer_v1", "invoice_agent.adapter:answer_v2"]),
        (
            "invoice_agent_safety_v1.yaml",
            ["invoice_agent.adapter:answer_v1_without_d2", "invoice_agent.adapter:answer_v2"],
        ),
        ("rag_support_v1.yaml", ["rag_app.adapter:answer"]),
        ("fault_injection_demo.yaml", ["rag_app.fault_injection:answer"]),
    ],
)
async def test_replay_without_overrides_reproduces_every_case(run, replay, file: str, targets: list[str]) -> None:
    replayed = 0
    for target in targets:
        original_run = await run(file, target)
        for original in original_run["results"]:
            if original["status"] == "timeout":
                continue  # a timeout's outcome is the clock's, not the agent's
            r = await replay(original["id"])
            assert r["status"] == "completed", r["error_message"]
            diff = r["diff"]
            assert diff["identical"] is True, (target, original["case_key"], diff["differences"])
            assert r["result_status"] == original["status"] and r["passed"] == original["passed"]
            assert r["output_answer"] == original["output_answer"]
            assert r["error_type"] == original["error_type"]
            assert _steps(r["steps"]) == _steps(original["steps"])
            assert _verdicts(r["metrics"]) == _verdicts(original["metrics"])
            assert r["retrieved_doc_ids"] == original["retrieved_doc_ids"]
            assert (r["input_tokens"], r["output_tokens"]) == (original["input_tokens"], original["output_tokens"])
            replayed += 1
    assert replayed > 0


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"temperature": 0.2}, "unknown setting 'temperature' (accepted: behavior, d1, d2, d3, d4, d5)"),
        # What --prompt-file sends: the invoice agent's planner is scripted, so it declares no prompt.
        ({"prompt": "You are a careful billing agent."}, "unknown setting 'prompt'"),
        ({"d2": "maybe"}, "'d2' must be on/off (true/false), got 'maybe'"),
        ({"behavior": "v3"}, "'behavior' must be one of v1, v2, v1-without-d2; got 'v3'"),
    ],
)
async def test_overrides_the_adapter_did_not_declare_are_rejected(
    run, replay, client: AsyncClient, overrides: dict, message: str
) -> None:
    original_run = await run("invoice_agent_safety_v1.yaml", "invoice_agent.adapter:answer_v1_without_d2")
    original = _case(original_run, PR4_CASE)
    r = await replay(original["id"], overrides)
    assert r["status"] == "failed"
    assert r["error_message"].startswith("overrides rejected: adapter 'invoice_agent.adapter:answer_v1_without_d2':")
    assert message in r["error_message"]
    assert r["steps"] == [] and r["metrics"] == [] and r["diff"] is None and r["result_status"] is None
    # The agent never ran: only the API's and the worker's replay spans exist.
    [root] = r["spans"]
    assert [c["name"] for c in root["children"]] == ["agentforge.replay"]
    assert root["children"][0]["children"] == []
    assert (await client.get(f"/runs/{original_run['id']}")).json() == original_run


async def test_an_adapter_without_declared_settings_takes_no_overrides(run, replay) -> None:
    original_run = await run("fault_injection_demo.yaml", "rag_app.fault_injection:answer")
    r = await replay(original_run["results"][0]["id"], {"top_k": 3})
    assert r["status"] == "failed"
    assert "adapter 'rag_app.fault_injection:answer' declares no replay settings" in r["error_message"]
    assert "replay it without overrides" in r["error_message"]


async def test_rag_retrieval_overrides(run, replay) -> None:
    original_run = await run("rag_support_v1.yaml", "rag_app.adapter:answer")
    original = max(original_run["results"], key=lambda r: len(r["retrieved_doc_ids"]))
    assert len(original["retrieved_doc_ids"]) == 2  # TOP_K

    wider = await replay(original["id"], {"top_k": 3})
    assert wider["status"] == "completed", wider["error_message"]
    assert wider["retrieved_doc_ids"][:2] == original["retrieved_doc_ids"]
    assert len(wider["retrieved_doc_ids"]) == 3
    assert "retrieved docs" in wider["diff"]["differences"]

    narrow = await replay(original["id"], {"retrieval_config": {"top_k": 1}})
    assert narrow["retrieved_doc_ids"] == original["retrieved_doc_ids"][:1]

    both = await replay(original["id"], {"top_k": 1, "retrieval_config": {"top_k": 2}})
    assert both["status"] == "failed" and "top_k is set both directly and in retrieval_config" in both["error_message"]
    bad = await replay(original["id"], {"retrieval_config": {"min_score": 99, "rerank": True}})
    assert bad["status"] == "failed"
    assert "'retrieval_config.min_score' must be <= 20, got 99" in bad["error_message"]
    assert "unknown setting 'retrieval_config.rerank' (accepted: min_score, top_k)" in bad["error_message"]


async def test_replay_requests_the_api_rejects(
    run, client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    missing = await client.post("/replay", json={"result_id": "00000000-0000-0000-0000-000000000000"})
    assert missing.status_code == 404
    assert (await client.get("/replays/nope")).status_code == 404
    assert (await client.get("/results/nope/replays")).status_code == 404
    bad_name = await client.post("/replay", json={"result_id": "x", "overrides": {"not a name": 1}})
    assert bad_name.status_code == 422

    # An HTTP adapter can't declare settings: overrides are refused before anything is queued.
    http_run = await run("rag_support_v1.yaml", "http://127.0.0.1:1/answer", adapter_type="http")
    result_id = http_run["results"][0]["id"]
    refused = await client.post("/replay", json={"result_id": result_id, "overrides": {"top_k": 3}})
    assert refused.status_code == 400
    assert "http adapter, which can't declare replay settings" in refused.json()["detail"]
    assert queue.enqueued_replays == []
    assert (await client.post("/replay", json={"result_id": result_id})).status_code == 202

    # A result of a run that's still running: not replayable until the run finishes.
    pending = await client.post(
        "/runs",
        json={
            "application_id": http_run["application_id"],
            "application_version_id": http_run["application_version_id"],
            "dataset_version_id": http_run["dataset_version_id"],
            "adapter": {"type": "python", "target": "rag_app.adapter:answer"},
        },
    )
    async with session_factory() as session:
        running = await session.get(EvaluationRun, pending.json()["id"])
        assert running is not None
        transition(running, RunStatus.running)
        partial = EvaluationResult(
            evaluation_run_id=running.id, test_case_id=http_run["results"][0]["test_case_id"], latency_ms=1.0
        )
        session.add(partial)
        await session.commit()
    unfinished = await client.post("/replay", json={"result_id": partial.id})
    assert unfinished.status_code == 409 and "is still running" in unfinished.json()["detail"]

    # Redis down: the replay is created, marked failed, and the caller gets a 503.
    queue.fail_with = "redis unreachable"
    down = await client.post("/replay", json={"result_id": result_id})
    assert down.status_code == 503
    [newest, *_] = (await client.get(f"/results/{result_id}/replays")).json()
    assert newest["status"] == "failed" and "not executed: redis unreachable" in newest["error_message"]


async def test_finished_replays_are_immutable(
    run, replay, session_factory: async_sessionmaker[AsyncSession], queue
) -> None:
    original_run = await run("invoice_agent_v1.yaml", "invoice_agent.adapter:answer_v1")
    r = await replay(original_run["results"][0]["id"])
    assert r["status"] == "completed"

    # Service layer: no way out of a terminal status, no outcome writes.
    async with session_factory() as session:
        row = await session.get(Replay, r["id"])
        assert row is not None
        with pytest.raises(replay_service.ReplayStateError):
            replay_service.transition(row, "running")
        with pytest.raises(replay_service.ReplayStateError):
            replay_service.assert_accepts_outcome(row)
    # A re-delivered job is a no-op.
    assert await execute_replay(session_factory, r["id"]) == "skipped"


@pytest.mark.skipif(not os.environ.get(POSTGRES), reason="DB triggers exist only in the migrated Postgres schema")
async def test_db_triggers_freeze_a_finished_replay(run, replay, session_factory) -> None:
    original_run = await run("invoice_agent_v1.yaml", "invoice_agent.adapter:answer_v1")
    r = await replay(original_run["results"][0]["id"])
    rid = r["id"]
    statements = [
        f"UPDATE replays SET passed = NOT passed WHERE id = '{rid}'",
        f"DELETE FROM replays WHERE id = '{rid}'",
        f"UPDATE replay_steps SET name = 'x' WHERE replay_id = '{rid}'",
        f"DELETE FROM replay_steps WHERE replay_id = '{rid}'",
        f"INSERT INTO replay_steps (id, replay_id, step_index, kind, name, args, retrieved_doc_ids, created_at) "
        f"VALUES ('s-new', '{rid}', 99, 'tool_call', 'x', '{{}}', '[]', now())",
        f"UPDATE replay_metric_scores SET passed = NOT passed WHERE replay_id = '{rid}'",
        f"DELETE FROM replay_metric_scores WHERE replay_id = '{rid}'",
        f"UPDATE replay_spans SET name = 'x' WHERE replay_id = '{rid}'",
        f"DELETE FROM replay_spans WHERE replay_id = '{rid}'",
    ]
    for stmt in statements:
        async with session_factory() as session:
            with pytest.raises((IntegrityError, DBAPIError), match="immutable"):
                await session.execute(text(stmt))
                await session.commit()

    # Control: a pending replay's row can still be written (by the worker, when it starts).
    pending = Replay(
        original_result_id=r["original_result_id"],
        original_run_id=r["original_run_id"],
        test_case_id=r["test_case_id"],
        dataset_version_id=r["dataset_version_id"],
        adapter_type="python",
        adapter_target="invoice_agent.adapter:answer_v1",
        evaluators=[],
        provider_type="local-deterministic",
        threshold=0.7,
        overrides={},
    )
    async with session_factory() as session:
        session.add(pending)
        await session.commit()
        await session.execute(text(f"UPDATE replays SET status = 'running' WHERE id = '{pending.id}'"))
        session.add(ReplayStep(replay_id=pending.id, step_index=1, kind="final_answer", output="x"))
        await session.commit()
