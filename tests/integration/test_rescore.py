"""Re-scores through the real API and the worker's real execute_rescore.

* A run's stored outputs scored again with other evaluator versions: verdicts change where the new
  versions judge differently, nothing calls the adapter or the model, and the re-score links back to
  the original run and results, which are left exactly as they were.
* The scripted agents keep every verdict when @1.0.0 answer checks are swapped for @1.1.0.
* Requests the API refuses, and finished re-scores are frozen (service layer, and DB triggers on Postgres).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from llm_fakes import turn, use
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_api.models.rescore import Rescore
from agentforge_api.services import rescores as rescore_service
from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_evaluators import referenced_keys, resolve
from agentforge_worker.rescore import execute_rescore
from agentforge_worker.runner import execute_run

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[2]
POSTGRES = "AGENTFORGE_TEST_DATABASE_URL"
LLM_TARGET = "invoice_agent.adapter:answer_llm"
HAIKU = {"provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.0}
NEW = ["answer_contains@1.1.0", "answer_regex@1.1.0", "graceful_tool_failure@1.1.0"]

# Two LLM-style answers the two versions judge oppositely (checked offline against the evaluators):
#  * a refusal worded unlike the scripted agent's: answer_regex 1.0.0 FAIL (no literal match), 1.1.0 PASS
#  * "voided" inside a negation, with no void call: answer_contains 1.0.0 PASS (substring), 1.1.0 FAIL
OUT_OF_SCOPE = "Update the credit card on file for Kestrel Robotics."
VOID = "Void INV-1006, it's a duplicate of INV-1001."
ANSWERS = {
    OUT_OF_SCOPE: "I can't help with changing a card on file; that's outside what I handle.",
    VOID: "I haven't voided INV-1006. Please confirm it's a duplicate first.",
}
CASES = [
    {
        "case_key": "oos-001",
        "input": OUT_OF_SCOPE,
        "trajectory": {"expected_tools": []},
        "evaluators": {"answer_regex": {"pattern": "(?i)can't make that change"}},
    },
    {
        "case_key": "void-001",
        "input": VOID,
        "trajectory": {"expected_tools": []},
        "evaluators": {"answer_contains": {"phrases": ["voided"]}},
    },
]


class AnswerByInput:
    """A fake model that answers each request with a fixed final text, no tool calls. `calls` counts it."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, config, system, messages, tools):
        self.calls += 1
        request = next(m.text for m in messages if m.role == "user")
        return turn(text=ANSWERS[request])


def pinned_at_1_0_0(file: str) -> list[str]:
    """The evaluators a run of this dataset pins by default, with the three re-versioned checks at 1.0.0."""
    _name, _d, cases, defaults = validate_dataset_file(REPO / "datasets" / file)
    keys = referenced_keys(defaults, [c.evaluators for c in cases]) or []
    out = []
    for key in keys:
        name = resolve(key).name
        out.append(f"{name}@1.0.0" if f"{name}@1.1.0" in NEW else resolve(key).key)
    return out


@pytest_asyncio.fixture
async def app_version(client: AsyncClient) -> tuple[str, str]:
    app = (await client.post("/applications", json={"name": "rescore-test"})).json()
    version = (await client.post(f"/applications/{app['id']}/versions", json={"version": "v"})).json()
    return app["id"], version["id"]


async def publish(client: AsyncClient, name: str, cases: list[dict], defaults: dict | None) -> str:
    ds = (await client.post("/datasets", json={"name": name})).json()
    body = {"test_cases": cases, "default_evaluators": defaults}
    resp = await client.post(f"/datasets/{ds['id']}/versions", json=body)
    assert resp.status_code == 200, resp.text
    v = resp.json()
    await client.post(f"/datasets/{name}/versions/{v['version']}/publish")
    return v["id"]


async def create_run(
    client: AsyncClient,
    app_version: tuple[str, str],
    dataset_version_id: str,
    target: str,
    evaluators: list[str],
    settings: dict[str, Any] | None = None,
) -> str:
    resp = await client.post(
        "/runs",
        json={
            "application_id": app_version[0],
            "application_version_id": app_version[1],
            "dataset_version_id": dataset_version_id,
            "adapter": {"type": "python", "target": target},
            "provider_type": "llm:anthropic" if target == LLM_TARGET else "local-deterministic",
            "timeout_seconds": 10,
            "evaluators": evaluators,
            "adapter_settings": settings,
        },
    )
    assert resp.status_code == 202, resp.text
    run_id: str = resp.json()["id"]
    return run_id


@pytest_asyncio.fixture
async def rescore(client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]):
    """rescore(run id, evaluators) -> the finished re-score's JSON (POST, then the worker)."""

    async def go(run_id: str, evaluators: list[str]) -> dict:
        resp = await client.post(f"/runs/{run_id}/rescores", json={"evaluators": evaluators})
        assert resp.status_code == 202, resp.text
        created = resp.json()
        assert created["status"] == "pending" and created["results"] == []
        assert queue.enqueued_rescores[-1] == created["id"]
        await execute_rescore(session_factory, created["id"])
        return (await client.get(f"/rescores/{created['id']}")).json()

    return go


def _verdicts(results: list[dict]) -> dict[str, dict[str, Any]]:
    def per_case(r: dict) -> dict[str, Any]:
        return {m["evaluator_name"]: (m["evaluator_version"], m["passed"]) for m in r["metrics"]}

    return {r["case_key"]: per_case(r) for r in results}


async def test_rescore_changes_verdicts_without_calling_the_model(
    client: AsyncClient, queue, session_factory, app_version, rescore, monkeypatch
) -> None:
    model = use(monkeypatch, AnswerByInput())
    dv = await publish(client, "rescore-llm", CASES, {"tool_selection": {}})
    run_id = await create_run(
        client,
        app_version,
        dv,
        LLM_TARGET,
        ["tool_selection", "answer_contains@1.0.0", "answer_regex@1.0.0"],
        settings=HAIKU,
    )
    assert await execute_run(session_factory, run_id, trace_context=queue.trace_contexts[run_id]) == "completed"
    original = (await client.get(f"/runs/{run_id}")).json()
    assert model.calls == 2
    before = _verdicts(original["results"])
    assert before["oos-001"]["answer_regex"] == ("1.0.0", False)
    assert before["void-001"]["answer_contains"] == ("1.0.0", True)

    r = await rescore(run_id, ["answer_contains@1.1.0", "answer_regex"])  # a bare name is its latest version

    assert r["status"] == "completed", r["error_message"]
    assert model.calls == 2  # no model (or adapter) call
    assert r["original_run_id"] == run_id
    assert r["requested_evaluators"] == ["answer_contains@1.1.0", "answer_regex"]
    assert r["evaluators"] == [resolve("tool_selection").key, "answer_contains@1.1.0", "answer_regex@1.1.0"]
    assert r["replaced"] == {
        "answer_contains": "answer_contains@1.0.0 -> answer_contains@1.1.0",
        "answer_regex": "answer_regex@1.0.0 -> answer_regex@1.1.0",
    }
    assert {x["original_result_id"] for x in r["results"]} == {x["id"] for x in original["results"]}
    after = _verdicts(r["results"])
    assert after["oos-001"]["answer_regex"] == ("1.1.0", True)
    assert after["void-001"]["answer_contains"] == ("1.1.0", False)
    assert after["oos-001"]["tool_selection"] == before["oos-001"]["tool_selection"]
    by_case = {x["case_key"]: x for x in r["results"]}
    assert (by_case["oos-001"]["original_passed"], by_case["oos-001"]["passed"], by_case["oos-001"]["changed"]) == (
        False,
        True,
        True,
    )
    assert (by_case["void-001"]["original_passed"], by_case["void-001"]["passed"]) == (True, False)
    assert by_case["void-001"]["model"] == original["results"][0]["model"]  # copied from the original result
    assert (r["newly_passing"], r["newly_failing"]) == (1, 1)
    assert r["aggregates"]["pass_rate"] == 0.5 and r["original_aggregates"] == original["aggregates"]

    # The original run is untouched, and lists its re-score.
    assert (await client.get(f"/runs/{run_id}")).json() == original
    listed = (await client.get(f"/runs/{run_id}/rescores")).json()
    assert [x["id"] for x in listed] == [r["id"]] and listed[0]["newly_failing"] == 1


@pytest.mark.parametrize(
    ("file", "targets"),
    [
        ("invoice_agent_v1.yaml", ["answer_v1", "answer_v2"]),
        ("invoice_agent_safety_v1.yaml", ["answer_v1", "answer_v2", "answer_v1_without_d2"]),
    ],
)
async def test_scripted_agents_keep_every_verdict_under_1_1_0(
    client: AsyncClient, queue, session_factory, app_version, rescore, file: str, targets: list[str]
) -> None:
    name, _d, cases, defaults = validate_dataset_file(REPO / "datasets" / file)
    dv = await publish(client, name, [c.model_dump() for c in cases], defaults)
    for target in targets:
        run_id = await create_run(client, app_version, dv, f"invoice_agent.adapter:{target}", pinned_at_1_0_0(file))
        assert await execute_run(session_factory, run_id, trace_context=queue.trace_contexts[run_id]) == "completed"
        original = (await client.get(f"/runs/{run_id}")).json()
        r = await rescore(run_id, NEW)
        assert r["status"] == "completed", r["error_message"]
        assert set(r["replaced"]) <= {"answer_contains", "answer_regex", "graceful_tool_failure"}
        assert (r["newly_passing"], r["newly_failing"]) == (0, 0), target
        assert r["aggregates"]["pass_rate"] == original["aggregates"]["pass_rate"]
        assert r["aggregates"].get("safety") == original["aggregates"].get("safety")
        before = {x["case_key"]: x for x in original["results"]}
        for x in r["results"]:
            old = {m["evaluator_name"]: m["passed"] for m in before[x["case_key"]]["metrics"]}
            new = {m["evaluator_name"]: m["passed"] for m in x["metrics"]}
            assert new == old, (target, x["case_key"])


async def test_rescore_requests_the_api_rejects(client: AsyncClient, queue, session_factory, app_version) -> None:
    name, _d, cases, defaults = validate_dataset_file(REPO / "datasets" / "invoice_agent_v1.yaml")
    dv = await publish(client, name, [c.model_dump() for c in cases], defaults)
    pending = await create_run(client, app_version, dv, "invoice_agent.adapter:answer_v1", [])

    assert (await client.post("/runs/nope/rescores", json={"evaluators": NEW})).status_code == 404
    assert (await client.get("/runs/nope/rescores")).status_code == 404
    assert (await client.get("/rescores/nope")).status_code == 404
    not_done = await client.post(f"/runs/{pending}/rescores", json={"evaluators": NEW})
    assert not_done.status_code == 409 and "only a completed run" in not_done.json()["detail"]
    assert (await client.post(f"/runs/{pending}/rescores", json={"evaluators": []})).status_code == 422

    assert await execute_run(session_factory, pending, trace_context=queue.trace_contexts[pending]) == "completed"
    unknown = await client.post(f"/runs/{pending}/rescores", json={"evaluators": ["answer_contains@9.9.9"]})
    assert unknown.status_code == 400
    assert (await client.get(f"/runs/{pending}/rescores")).json() == []  # nothing persisted for a 400

    queue.fail_with = "redis unreachable"
    down = await client.post(f"/runs/{pending}/rescores", json={"evaluators": NEW})
    assert down.status_code == 503
    [newest] = (await client.get(f"/runs/{pending}/rescores")).json()
    assert newest["status"] == "failed" and "not executed: redis unreachable" in newest["error_message"]


async def test_finished_rescores_are_immutable(
    client: AsyncClient, queue, session_factory, app_version, rescore
) -> None:
    name, _d, cases, defaults = validate_dataset_file(REPO / "datasets" / "invoice_agent_v1.yaml")
    dv = await publish(client, name, [c.model_dump() for c in cases], defaults)
    run_id = await create_run(client, app_version, dv, "invoice_agent.adapter:answer_v1", [])
    await execute_run(session_factory, run_id, trace_context=queue.trace_contexts[run_id])
    r = await rescore(run_id, NEW)
    assert r["status"] == "completed"

    async with session_factory() as session:
        row = await session.get(Rescore, r["id"])
        assert row is not None
        with pytest.raises(rescore_service.RescoreStateError):
            rescore_service.transition(row, "running")
        with pytest.raises(rescore_service.RescoreStateError):
            rescore_service.assert_accepts_outcome(row)
    # A re-delivered job is a no-op.
    assert await execute_rescore(session_factory, r["id"]) == "skipped"
    assert (await client.get(f"/rescores/{r['id']}")).json() == r


@pytest.mark.skipif(not os.environ.get(POSTGRES), reason="DB triggers exist only in the migrated Postgres schema")
async def test_db_triggers_freeze_a_finished_rescore(
    client: AsyncClient, queue, session_factory, app_version, rescore
) -> None:
    name, _d, cases, defaults = validate_dataset_file(REPO / "datasets" / "invoice_agent_v1.yaml")
    dv = await publish(client, name, [c.model_dump() for c in cases], defaults)
    run_id = await create_run(client, app_version, dv, "invoice_agent.adapter:answer_v1", [])
    await execute_run(session_factory, run_id, trace_context=queue.trace_contexts[run_id])
    r = await rescore(run_id, NEW)
    rid = r["id"]
    some_result = r["results"][0]["original_result_id"]
    statements = [
        f"UPDATE rescores SET status = 'running' WHERE id = '{rid}'",
        f"DELETE FROM rescores WHERE id = '{rid}'",
        f"UPDATE rescore_results SET passed = NOT passed WHERE rescore_id = '{rid}'",
        f"DELETE FROM rescore_results WHERE rescore_id = '{rid}'",
        f"INSERT INTO rescore_results (id, rescore_id, original_result_id, passed, created_at) "
        f"VALUES ('r-new', '{rid}', '{some_result}', true, now())",
        f"UPDATE rescore_metric_scores SET passed = NOT passed WHERE rescore_id = '{rid}'",
        f"DELETE FROM rescore_metric_scores WHERE rescore_id = '{rid}'",
    ]
    for stmt in statements:
        async with session_factory() as session:
            with pytest.raises((IntegrityError, DBAPIError), match="immutable"):
                await session.execute(text(stmt))
                await session.commit()

    # Control: a pending re-score can still be written (the worker moves it to running).
    async with session_factory() as session:
        pending = Rescore(original_run_id=run_id, requested_evaluators=NEW, evaluators=NEW, replaced={})
        session.add(pending)
        await session.commit()
        rescore_service.transition(pending, "running")
        await session.commit()
