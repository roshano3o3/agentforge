"""LLM-planner runs through the real API and the worker's execute_run / execute_replay.

No network and no keys: the model is a fake (tests/llm_fakes.py). What's real
is everything around it -- run creation with adapter settings and a cost cap,
the worker validating the settings and passing them to every case, the cost
cap stopping a run, replays starting from the run's settings, and keys never
reaching a stored row or span.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openai
import pytest
import pytest_asyncio
from httpx import AsyncClient
from llm_fakes import PlannerBackedModel, use
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_evaluators import ModelPrice
from agentforge_worker.replay import execute_replay
from agentforge_worker.runner import execute_run
from invoice_agent.llm_planner import D2_TEXT

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[2]
LLM_TARGET = "invoice_agent.adapter:answer_llm"
HAIKU = {"provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.0}
PRICES = {"claude-haiku-4-5": ModelPrice(1.0, 5.0)}


@pytest_asyncio.fixture
async def submit(client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]):
    """submit(dataset file, target, settings=None, max_cost=None, pricing=None) -> (status, run JSON)."""
    app = (await client.post("/applications", json={"name": "llm-test"})).json()
    version = (await client.post(f"/applications/{app['id']}/versions", json={"version": "v"})).json()
    published: dict[str, str] = {}

    async def go(
        file: str,
        target: str,
        settings: dict[str, Any] | None = None,
        max_cost: float | None = None,
        pricing: dict[str, ModelPrice] | None = None,
    ) -> tuple[str, dict]:
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
                "provider_type": "llm:anthropic" if target == LLM_TARGET else "local-deterministic",
                "timeout_seconds": 10,
                "adapter_settings": settings,
                "max_cost_usd": max_cost,
            },
        )
        assert resp.status_code == 202, resp.text
        run_id = resp.json()["id"]
        status = await execute_run(session_factory, run_id, pricing=pricing, trace_context=queue.trace_contexts[run_id])
        return status, (await client.get(f"/runs/{run_id}")).json()

    return go


async def test_an_llm_run_scores_like_any_other_run(monkeypatch, submit, client: AsyncClient) -> None:
    model = use(monkeypatch, PlannerBackedModel(input_tokens=120, output_tokens=30))
    status, run = await submit("invoice_agent_v1.yaml", LLM_TARGET, HAIKU, pricing=PRICES)
    assert status == "completed", run["error_message"]
    assert run["adapter_settings"] == HAIKU
    assert run["labels"] == []  # a model's answers, not fixture output (the tasks are still synthetic)
    # The stand-in "model" makes v1's scripted decisions, so the run reproduces v1's 10/10 through the LLM path.
    assert run["aggregates"]["pass_rate"] == 1.0 and run["aggregates"]["case_count"] == 10
    turns_per_case = [sum(1 for s in r["steps"]) for r in run["results"]]  # tool calls + the final answer = turns
    assert model.calls == sum(turns_per_case)
    for r, turns in zip(run["results"], turns_per_case, strict=True):
        assert r["model"] == "claude-haiku-4-5"
        assert (r["input_tokens"], r["output_tokens"]) == (120 * turns, 30 * turns)

    # The run's settings become what a replay starts from.
    options = (await client.get(f"/results/{run['results'][0]['id']}/replay-options")).json()
    defaults = {s["name"]: s["default"] for s in options["settings"]}
    assert defaults["provider"] == "anthropic" and defaults["model"] == "claude-haiku-4-5" and defaults["d2"] is True
    assert options["recorded"] is True and options["overrides_supported"] is True


async def test_settings_are_checked_before_any_case(monkeypatch, submit) -> None:
    use(monkeypatch, PlannerBackedModel())
    status, run = await submit("invoice_agent_v1.yaml", LLM_TARGET, {"provider": "bedrock", "model": "x"})
    assert status == "failed" and run["results"] == []
    assert run["error_message"].startswith("adapter settings rejected: adapter 'invoice_agent.adapter:answer_llm':")
    assert "'provider' must be one of openai, anthropic, ollama" in run["error_message"]
    # The scripted planner declares no model to set.
    status, run = await submit("invoice_agent_v1.yaml", "invoice_agent.adapter:answer_v1", {"model": "x"})
    assert status == "failed" and "unknown setting 'model'" in run["error_message"]


async def test_http_adapters_take_no_settings(client: AsyncClient, submit) -> None:
    status, run = await submit("invoice_agent_v1.yaml", "invoice_agent.adapter:answer_v1")
    resp = await client.post(
        "/runs",
        json={
            "application_id": run["application_id"],
            "application_version_id": run["application_version_id"],
            "dataset_version_id": run["dataset_version_id"],
            "adapter": {"type": "http", "target": "http://127.0.0.1:1/answer"},
            "adapter_settings": {"model": "x"},
        },
    )
    assert resp.status_code == 400 and "python adapter" in resp.json()["detail"]


async def test_the_cost_cap_stops_a_run(monkeypatch, submit) -> None:
    use(monkeypatch, PlannerBackedModel(input_tokens=1000, output_tokens=100))  # >= $0.0015 per case at $1/$5
    status, run = await submit("invoice_agent_v1.yaml", LLM_TARGET, HAIKU, max_cost=0.003, pricing=PRICES)
    assert status == "failed"
    assert "passed the run's cost cap ($0.003)" in run["error_message"]
    assert 1 <= len(run["results"]) < 10  # the cases before the stop are kept
    spent = sum((r["input_tokens"] * 1.0 + r["output_tokens"] * 5.0) / 1e6 for r in run["results"])
    assert (
        spent > 0.003
        and spent - (run["results"][-1]["input_tokens"] + 5 * run["results"][-1]["output_tokens"]) / 1e6 <= 0.003
    )


async def test_a_capped_run_stops_when_spend_cant_be_measured(monkeypatch, submit) -> None:
    # The scripted planner reports no tokens: with a cap there's no way to enforce it.
    status, run = await submit("invoice_agent_v1.yaml", "invoice_agent.adapter:answer_v1", max_cost=1.0, pricing=PRICES)
    assert status == "failed" and len(run["results"]) == 1
    assert "this case's spend can't be measured: the adapter reported no token usage" in run["error_message"]
    # An unpriced model is the same.
    use(monkeypatch, PlannerBackedModel())
    status, run = await submit("invoice_agent_v1.yaml", LLM_TARGET, HAIKU, max_cost=1.0, pricing={})
    assert status == "failed" and "'claude-haiku-4-5' has no entry in the pricing file" in run["error_message"]


async def test_a_replay_starts_from_the_runs_settings(
    monkeypatch, submit, client: AsyncClient, queue, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    model = use(monkeypatch, PlannerBackedModel())
    status, run = await submit("invoice_agent_v1.yaml", LLM_TARGET, HAIKU, pricing=PRICES)
    assert status == "completed"
    result = run["results"][0]

    async def replay(overrides: dict) -> dict:
        created = (await client.post("/replay", json={"result_id": result["id"], "overrides": overrides})).json()
        await execute_replay(
            session_factory, created["id"], pricing=PRICES, trace_context=queue.trace_contexts[created["id"]]
        )
        return (await client.get(f"/replays/{created['id']}")).json()

    model.systems.clear()
    plain = await replay({})
    assert plain["status"] == "completed", plain["error_message"]
    assert plain["overrides"] == {}
    assert plain["diff"]["identical"] is True  # same fake decisions, same settings
    assert all(D2_TEXT in s for s in model.systems)

    model.systems.clear()
    no_d2 = await replay({"d2": False})
    assert no_d2["status"] == "completed" and no_d2["overrides"] == {"d2": False}
    assert model.systems and all(D2_TEXT not in s for s in model.systems)


async def test_keys_never_reach_results_or_spans(monkeypatch, submit, client: AsyncClient) -> None:
    key = "sk-proj-FAKEKEYFORTHISTEST0123456789abcdef"
    monkeypatch.setenv("OPENAI_API_KEY", key)

    class Rejecting:
        def __init__(self, **kwargs: Any) -> None:
            self.chat = self
            self.completions = self

        def create(self, **kwargs: Any) -> Any:
            raise RuntimeError(f"Error code: 401 - Incorrect API key provided: {key}")

    monkeypatch.setattr(openai, "OpenAI", Rejecting)
    settings = {"provider": "openai", "model": "gpt-5.4-mini"}
    status, run = await submit("invoice_agent_v1.yaml", LLM_TARGET, settings)
    assert status == "completed"
    assert {r["status"] for r in run["results"]} == {"error"}
    assert "[REDACTED]" in run["results"][0]["error_message"]
    assert key not in json.dumps(run)
    traces = [await client.get(f"/traces/{r['id']}") for r in run["results"]]
    assert all(t.status_code == 200 for t in traces)  # each case's spans were stored...
    assert all("planner_decision" in t.text for t in traces)  # ...including the failed model call's span
    assert not any(key in t.text for t in traces)  # ...and none carries the key
