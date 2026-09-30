"""Per-case evaluator config: validation on write, what a run applies to each
case, PATCH/new-draft behavior, and Phase 2 legacy datasets."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_run_lifecycle import PRICING, _create, _payload, _setup

from agentforge_api.models.dataset import TestCase as _TestCaseRow
from agentforge_worker.runner import execute_run

pytestmark = pytest.mark.asyncio

DEFAULT = {"heuristic_context_precision": {}, "latency": {}}
CASES = [
    {
        "case_key": "a-warranty",
        "input": "Do you offer a warranty on outerwear jackets?",
        "expected_context": ["policy-warranty-003"],
        "evaluators": {"answer_regex": {"pattern": r"\b2 year\b"}},
    },
    {
        "case_key": "b-out-of-scope",
        "input": "Do you sponsor any professional cycling teams?",
        "expected_context": [],
        "evaluators": {"heuristic_context_precision": False, "answer_contains": {"phrases": ["could not find"]}},
    },
    {
        "case_key": "c-defaults-only",
        "input": "How long does standard shipping take?",
        "expected_context": ["policy-shipping-002"],
    },
]


def _applied(result: dict) -> set[str]:
    return {m["evaluator_name"] for m in result["metrics"]}


async def test_a_run_applies_exactly_each_cases_configured_evaluators(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client, CASES, default_evaluators=DEFAULT)
    run = await _create(client, _payload(ids))

    # Pinned: exactly the evaluators the configs reference -- not all nine.
    assert sorted(run["evaluators"]) == [
        "answer_contains@1.0.0",
        "answer_regex@1.0.0",
        "heuristic_context_precision@1.0.0",
        "latency@1.0.0",
    ]
    assert await execute_run(session_factory, run["id"], pricing=PRICING) == "completed"
    done = (await client.get(f"/runs/{run['id']}")).json()
    by_case = {r["case_key"]: r for r in done["results"]}

    assert _applied(by_case["a-warranty"]) == {"heuristic_context_precision", "latency", "answer_regex"}
    assert _applied(by_case["b-out-of-scope"]) == {"latency", "answer_contains"}  # precision dropped
    assert _applied(by_case["c-defaults-only"]) == {"heuristic_context_precision", "latency"}

    regex = next(m for m in by_case["a-warranty"]["metrics"] if m["evaluator_name"] == "answer_regex")
    assert regex["passed"] is True
    assert regex["evidence"]["params"] == {"pattern": r"\b2 year\b"}  # params recorded with the result
    contains = next(m for m in by_case["b-out-of-scope"]["metrics"] if m["evaluator_name"] == "answer_contains")
    assert contains["passed"] is True

    # Aggregates only count cases an evaluator actually applied to.
    metrics = done["aggregates"]["metrics"]
    assert metrics["heuristic_context_precision@1.0.0"]["scored_count"] == 2
    assert metrics["answer_regex@1.0.0"]["scored_count"] == 1
    assert "exact_match@1.0.0" not in metrics


async def test_explicit_evaluator_list_filters_the_configured_set(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _setup(client, CASES, default_evaluators=DEFAULT)
    run = await _create(client, _payload(ids, evaluators=["latency"]))
    assert run["evaluators"] == ["latency@1.0.0"]
    await execute_run(session_factory, run["id"], pricing=PRICING)
    done = (await client.get(f"/runs/{run['id']}")).json()
    assert all(_applied(r) == {"latency"} for r in done["results"])


async def test_a_config_that_applies_nothing_is_rejected(client: AsyncClient, queue) -> None:
    ids = await _setup(client, [{"case_key": "k", "input": "q?"}], default_evaluators={})
    resp = await client.post("/runs", json=_payload(ids))
    assert resp.status_code == 400
    assert "applies no evaluators" in resp.json()["detail"]
    assert queue.enqueued == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"test_cases": [{"case_key": "k", "input": "q", "evaluators": {"made_up": {}}}]},
            "test case 'k': unknown evaluator",
        ),
        ({"test_cases": [{"case_key": "k", "input": "q", "evaluators": {"answer_contains": {}}}]}, "missing required"),
        (
            {"test_cases": [{"case_key": "k", "input": "q", "evaluators": {"answer_regex": {"pattern": "("}}}]},
            "regular expression",
        ),
        ({"test_cases": [], "default_evaluators": {"latency": False}}, "only makes sense in a test case"),
    ],
)
async def test_invalid_config_is_rejected_on_write(client: AsyncClient, body: dict, message: str) -> None:
    dataset = (await client.post("/datasets", json={"name": "ds"})).json()
    resp = await client.post(f"/datasets/{dataset['id']}/versions", json=body)
    assert resp.status_code == 422
    assert message in resp.json()["detail"]


async def test_retired_fields_are_rejected_not_silently_dropped(client: AsyncClient) -> None:
    dataset = (await client.post("/datasets", json={"name": "ds"})).json()
    resp = await client.post(
        f"/datasets/{dataset['id']}/versions",
        json={"test_cases": [{"case_key": "k", "input": "q", "expected_answer_contains": ["x"]}]},
    )
    assert resp.status_code == 422


async def test_patch_keeps_the_default_config_unless_it_is_sent(client: AsyncClient) -> None:
    dataset = (await client.post("/datasets", json={"name": "ds"})).json()
    await client.post(f"/datasets/{dataset['id']}/versions", json={"test_cases": [], "default_evaluators": DEFAULT})

    kept = await client.patch("/datasets/ds/versions/1", json={"test_cases": [{"case_key": "k", "input": "q"}]})
    assert kept.json()["default_evaluators"] == DEFAULT
    changed = await client.patch(
        "/datasets/ds/versions/1", json={"test_cases": [], "default_evaluators": {"latency": {"max_ms": 100}}}
    )
    assert changed.json()["default_evaluators"] == {"latency": {"max_ms": 100.0}}
    cleared = await client.patch("/datasets/ds/versions/1", json={"test_cases": [], "default_evaluators": None})
    assert cleared.json()["default_evaluators"] is None


async def test_phase2_legacy_answer_fields_still_apply_and_convert_on_new_draft(
    client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    # A version shaped like one published in Phase 2: no config at all, answer
    # assertions in the legacy columns (only writable directly in the DB now).
    ids = await _setup(
        client,
        [
            {
                "case_key": "warranty",
                "input": "Do you offer a warranty on outerwear jackets?",
                "expected_context": ["policy-warranty-003"],
            }
        ],
        publish=False,
    )
    async with session_factory() as session:
        await session.execute(
            update(_TestCaseRow)
            .where(_TestCaseRow.dataset_version_id == ids["dataset_version_id"])
            .values(expected_answer_contains=["2 year"])
        )
        await session.commit()
    assert (await client.post("/datasets/ds/versions/1/publish")).status_code == 200

    run = await _create(client, _payload(ids))
    await execute_run(session_factory, run["id"], pricing=PRICING)
    [result] = (await client.get(f"/runs/{run['id']}")).json()["results"]
    # No default config -> every evaluator, as in Phase 2; legacy phrases used as params.
    assert len(result["metrics"]) == 9
    contains = next(m for m in result["metrics"] if m["evaluator_name"] == "answer_contains")
    assert contains["passed"] is True
    assert contains["evidence"]["params"] == {"phrases": ["2 year"]}

    draft = (await client.post("/datasets/ds/versions/1/new-draft")).json()
    [tc] = draft["test_cases"]
    assert tc["evaluators"] == {"answer_contains": {"phrases": ["2 year"]}}
    assert tc["expected_answer_contains"] == []  # moved into config on the new draft
    async with session_factory() as session:
        [original] = (
            await session.scalars(
                select(_TestCaseRow).where(_TestCaseRow.dataset_version_id == ids["dataset_version_id"])
            )
        ).all()
        assert original.expected_answer_contains == ["2 year"]  # published row untouched
