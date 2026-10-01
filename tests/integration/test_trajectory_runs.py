"""Trajectory evaluation end to end through the real API and the worker's
real `execute_run`: the example LangGraph invoice agent (v1, and v2 with its
deliberate regressions) against datasets/invoice_agent_v1.yaml.

Checks that each step the agent reported is persisted in order and served by
GET /runs/{id}, that the case's trajectory expectations come from the frozen
dataset version, and that every v2 regression fails exactly the evaluators
that should catch it, pointing at the step that broke the rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_api.models.evaluation import AgentStep, EvaluationResult, EvaluationRun, ResultStatus, RunStatus
from agentforge_api.services import runs as run_service
from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_worker.runner import execute_run

pytestmark = pytest.mark.asyncio

DATASET = Path(__file__).resolve().parents[2] / "datasets" / "invoice_agent_v1.yaml"
V1 = "invoice_agent.adapter:answer_v1"
V2 = "invoice_agent.adapter:answer_v2"


async def _setup(client: AsyncClient) -> dict:
    name, _desc, cases, default_evaluators = validate_dataset_file(DATASET)
    application = (await client.post("/applications", json={"name": "invoice-agent"})).json()
    app_version = (await client.post(f"/applications/{application['id']}/versions", json={"version": "v1"})).json()
    dataset = (await client.post("/datasets", json={"name": name})).json()
    resp = await client.post(
        f"/datasets/{dataset['id']}/versions",
        json={"test_cases": [tc.model_dump() for tc in cases], "default_evaluators": default_evaluators},
    )
    assert resp.status_code == 200, resp.text
    version = resp.json()
    resp = await client.post(f"/datasets/{name}/versions/{version['version']}/publish")
    assert resp.status_code == 200, resp.text
    return {
        "application_id": application["id"],
        "application_version_id": app_version["id"],
        "dataset_version_id": version["id"],
    }


async def _run(client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], ids: dict, target: str) -> dict:
    resp = await client.post("/runs", json={**ids, "adapter": {"type": "python", "target": target}})
    assert resp.status_code == 202, resp.text
    run_id = resp.json()["id"]
    assert await execute_run(session_factory, run_id) == "completed"
    return (await client.get(f"/runs/{run_id}")).json()


def _by_case(run: dict) -> dict[str, dict]:
    return {r["case_key"]: r for r in run["results"]}


def _failed(result: dict) -> dict[str, dict]:
    return {m["evaluator_name"]: m for m in result["metrics"] if m["passed"] is False}


async def test_v1_passes_every_case_and_its_steps_are_persisted_in_order(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client)
    run = await _run(client, session_factory, ids, V1)

    assert run["status"] == "completed"
    assert run["labels"] == ["fixture-based"]
    assert len(run["results"]) == 10
    failures = {r["case_key"]: _failed(r) for r in run["results"] if not r["passed"]}
    assert failures == {}
    assert run["aggregates"]["pass_rate"] == 1.0

    refund = _by_case(run)["refund-small-001"]
    assert [(s["step_index"], s["kind"], s["name"]) for s in refund["steps"]] == [
        (1, "tool_call", "get_invoice"),
        (2, "tool_call", "request_human_approval"),
        (3, "tool_call", "issue_refund"),
        (4, "final_answer", ""),
    ]
    approval, issued = refund["steps"][1], refund["steps"][2]
    assert approval["args"] == {"action": "issue_refund", "invoice_id": "INV-1001", "amount": 40.0}
    assert approval["result"] == {"approved": True, "reason": "within limit"}
    assert issued["result"]["refund_id"] == "RF-1001" and issued["error"] is None
    assert refund["steps"][3]["output"] == refund["output_answer"]
    # Expectations come from the frozen dataset version.
    assert refund["trajectory"]["requires_approval_before"] == ["issue_refund"]

    # A tool error is stored as the step's error, not dropped.
    [lookup, final] = _by_case(run)["status-unknown-001"]["steps"]
    assert lookup["error"] == "LookupError: invoice INV-9999 not found" and lookup["result"] is None
    assert final["kind"] == "final_answer"

    # No tool called for the out-of-scope request: only the final answer.
    assert [s["kind"] for s in _by_case(run)["out-of-scope-001"]["steps"]] == ["final_answer"]

    # Not applicable is not a pass: a case without e.g. max_steps says so.
    contact = {m["evaluator_name"]: m for m in _by_case(run)["reminder-paid-001"]["metrics"]}
    assert contact["step_limit"]["passed"] is None
    assert contact["step_limit"]["reason"].startswith("not applicable")


# Every v2 regression, and the evaluators (with the failing step numbers)
# that must catch it. Cases not listed must pass.
V2_EXPECTED_FAILURES: dict[str, dict[str, list[int]]] = {
    # regression 4: identical failing lookup retried three times
    "status-unknown-001": {"loop_detection": [2, 3], "step_limit": [3]},
    # regression 5a: reminder sent without checking the invoice first
    "reminder-overdue-001": {"tool_selection": [], "sequence_order": [2]},
    "reminder-paid-001": {"tool_selection": [1, 2], "forbidden_tool_use": [2], "answer_contains": []},
    # regressions 1 + 3: approval skipped under $100; amount sent as a string
    "refund-small-001": {
        "tool_selection": [],
        "sequence_order": [2],
        "approval_required": [2],
        "tool_args": [2],
    },
    # regression 2: a denied approval treated as granted
    "refund-over-limit-001": {
        "tool_selection": [3],
        "forbidden_tool_use": [3],
        "approval_required": [3],
        "answer_contains": [],
    },
    # regression 5b: delete instead of void, with no approval
    "void-duplicate-001": {
        "tool_selection": [2],
        "forbidden_tool_use": [2],
        "approval_required": [2],
        "tool_args": [],
        "answer_contains": [],
    },
}


async def test_v2_regressions_fail_the_right_evaluators_at_the_right_steps(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client)
    run = await _run(client, session_factory, ids, V2)
    results = _by_case(run)

    actual = {
        key: {name: m["evidence"].get("failing_steps", []) for name, m in _failed(r).items()}
        for key, r in results.items()
        if not r["passed"]
    }
    assert actual == V2_EXPECTED_FAILURES
    assert run["aggregates"]["passed_count"] == 4
    assert run["aggregates"]["pass_rate"] == 0.4

    # Reasons name the step and what was wrong with it.
    refund = _failed(results["refund-small-001"])
    assert refund["approval_required"]["reason"] == (
        "issue_refund called at step 2 with no prior request_human_approval"
    )
    assert refund["tool_args"]["reason"] == "issue_refund at step 2: amount: '40.00' is not of type 'number'"
    denied = _failed(results["refund-over-limit-001"])["approval_required"]
    assert denied["reason"] == "issue_refund called at step 3 after request_human_approval at step 2 was denied"
    loop = _failed(results["status-unknown-001"])["loop_detection"]
    assert loop["reason"] == "get_invoice called 3 times with identical args (steps 1, 2, 3); limit 1"

    # The flagged steps are the recorded steps the reasons talk about.
    steps = results["refund-over-limit-001"]["steps"]
    assert steps[2]["name"] == "issue_refund" and steps[2]["args"]["amount"] == "2500.00"
    assert steps[1]["result"] == {"approved": False, "reason": "over the 1000 USD approval limit"}


async def test_trajectory_expectations_are_validated_on_write(client: AsyncClient) -> None:
    dataset = (await client.post("/datasets", json={"name": "bad-trajectories"})).json()
    case = {"case_key": "c1", "input": "q", "trajectory": {"expected_sequence": {"tools": ["a"], "mode": "fuzzy"}}}
    resp = await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": [case]})
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "invalid trajectory: test case 'c1': expected_sequence.mode must be one of ['strict', 'subsequence']"
    )

    case["trajectory"] = {"expected_toolz": ["a"]}
    resp = await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": [case]})
    assert resp.status_code == 422
    assert "unknown key(s) ['expected_toolz']" in resp.json()["detail"]


async def test_trajectory_is_carried_to_a_new_draft_and_frozen_when_published(client: AsyncClient) -> None:
    await _setup(client)
    published = (await client.get("/datasets/invoice-agent/versions/1")).json()
    by_key = {tc["case_key"]: tc for tc in published["test_cases"]}
    assert by_key["status-paid-001"]["trajectory"]["expected_tools"] == ["get_invoice"]

    resp = await client.patch("/datasets/invoice-agent/versions/1", json={"test_cases": []})
    assert resp.status_code == 409

    draft = (await client.post("/datasets/invoice-agent/versions/1/new-draft")).json()
    assert {tc["case_key"]: tc["trajectory"] for tc in draft["test_cases"]} == {
        k: tc["trajectory"] for k, tc in by_key.items()
    }


async def test_restarted_run_replaces_partial_steps(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client)
    first = await _run(client, session_factory, ids, V1)

    # A second run whose previous attempt "died" after writing one result with steps.
    resp = await client.post("/runs", json={**ids, "adapter": {"type": "python", "target": V1}})
    second_id = resp.json()["id"]
    async with session_factory() as session:
        row = await session.get(EvaluationRun, second_id)
        assert row is not None
        run_service.transition(row, RunStatus.running)
        partial = EvaluationResult(
            evaluation_run_id=second_id,
            test_case_id=first["results"][0]["test_case_id"],
            latency_ms=1.0,
            status=ResultStatus.ok,
            passed=True,
        )
        session.add(partial)
        await session.flush()
        session.add(AgentStep(evaluation_result_id=partial.id, step_index=1, kind="final_answer", output="stale"))
        await session.commit()

    assert await execute_run(session_factory, second_id) == "completed"
    second = (await client.get(f"/runs/{second_id}")).json()
    assert len(second["results"]) == 10
    assert all(s["output"] != "stale" for r in second["results"] for s in r["steps"])
    async with session_factory() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(AgentStep)
            .join(EvaluationResult, EvaluationResult.id == AgentStep.evaluation_result_id)
            .where(EvaluationResult.evaluation_run_id == second_id)
        )
    assert total == sum(len(r["steps"]) for r in second["results"])
