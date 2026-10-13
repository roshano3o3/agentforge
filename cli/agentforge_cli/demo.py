"""`agentforge demo seed`: load the demo into an empty AgentForge database, through the API only.

Everything it loads is produced the normal way: datasets published from the repo's YAML files, runs executed by
the worker, release decisions computed by the API from those runs, a replay executed by the worker. Nothing is
written to the database directly, and nothing is a canned number. The example agents are fixture-based (scripted
planner, no model), so every result is too.

What it loads, in order:

1. the datasets: `rag-support`, `invoice-agent` (trajectory, 10 cases), `invoice-agent-safety` (35 attack variants);
2. `rag-assistant@v1` on `rag-support`;
3. the production baseline: `invoice-agent@v1` on both invoice datasets, each set as `production`;
4. the candidate `invoice-agent@v2` (the deliberately regressed agent) on both, gated against production;
5. the candidate `invoice-agent@pr4-d2-off` (demo PR #4's change: v1 with only defense D2 off) on both, gated;
6. one replay of PR #4's failing indirect-injection case with D2 back on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_cli.release_io import load_config
from agentforge_sdk import AgentForgeClient

DATASET_FILES = ("rag_support_v1.yaml", "invoice_agent_v1.yaml", "invoice_agent_safety_v1.yaml")
TRAJECTORY = "invoice-agent"
SAFETY = "invoice-agent-safety"
ENVIRONMENT = "production"
REPLAY_CASE = "injection_indirect.memo-delete@get_invoice.contact-lookup-001"


@dataclass(frozen=True)
class DemoRun:
    app: str
    version: str
    dataset: str
    adapter: str


RAG_RUN = DemoRun("rag-assistant", "v1", "rag-support", "rag_app.adapter:answer")
BASELINE = [
    DemoRun("invoice-agent", "v1", TRAJECTORY, "invoice_agent.adapter:answer_v1"),
    DemoRun("invoice-agent", "v1", SAFETY, "invoice_agent.adapter:answer_v1"),
]
CANDIDATES = [
    DemoRun("invoice-agent", "v2", TRAJECTORY, "invoice_agent.adapter:answer_v2"),
    DemoRun("invoice-agent", "v2", SAFETY, "invoice_agent.adapter:answer_v2"),
    DemoRun("invoice-agent", "pr4-d2-off", TRAJECTORY, "invoice_agent.adapter:answer_v1_without_d2"),
    DemoRun("invoice-agent", "pr4-d2-off", SAFETY, "invoice_agent.adapter:answer_v1_without_d2"),
]
POLICY = {TRAJECTORY: "release_policy", SAFETY: "safety_policy"}


class SeedError(RuntimeError):
    pass


def _wait(fetch: Callable[[], dict[str, Any]], what: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    current = fetch()
    while current["status"] not in {"completed", "failed"}:
        if time.monotonic() > deadline:
            raise SeedError(f"{what} still {current['status']} after {timeout:g}s (is the worker running?)")
        time.sleep(0.5)
        current = fetch()
    if current["status"] == "failed":
        raise SeedError(f"{what} failed: {current.get('error_message') or current.get('error')}")
    return current


def wait_for_api(client: AgentForgeClient, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            client.list_runs()
            return
        except Exception as exc:  # noqa: BLE001 - not up yet
            if time.monotonic() > deadline:
                raise SeedError(f"the API didn't answer within {timeout:g}s: {exc}") from None
            time.sleep(1)


def is_empty(client: AgentForgeClient) -> bool:
    return not client.list_applications() and not client.list_runs()


def seed(
    client: AgentForgeClient, root: Path, say: Callable[[str], None], *, run_timeout: float = 300.0
) -> dict[str, Any]:
    """Load the demo (module docstring). Returns the ids it created. Assumes an empty database."""
    project = load_config(root / "agentforge.yaml")
    for file in DATASET_FILES:
        name, description, cases, defaults = validate_dataset_file(root / "datasets" / file)
        dataset = client.upsert_dataset(name=name, description=description)
        draft = client.create_draft_version(
            dataset_id=dataset["id"], test_cases=[c.model_dump() for c in cases], default_evaluators=defaults
        )
        published = client.publish_dataset_version(dataset_name=name, version=draft["version"])
        say(f"dataset {name} v{published['version']}: {len(cases)} case(s), {published['content_hash']}")

    def run(spec: DemoRun) -> dict[str, Any]:
        application = client.upsert_application(name=spec.app)
        version = client.upsert_application_version(application_id=application["id"], version=spec.version)
        dataset_version = client.get_dataset_version(spec.dataset, "latest")
        created = client.create_run(
            {
                "application_id": application["id"],
                "application_version_id": version["id"],
                "dataset_version_id": dataset_version["id"],
                "adapter": {"type": "python", "target": spec.adapter},
                "evaluators": None,
                "provider_type": "local-deterministic",
                "threshold": 0.7,
                "timeout_seconds": 10.0,
                "environment": "demo",
            }
        )
        what = f"run {spec.app}@{spec.version} on {spec.dataset}"
        done = _wait(lambda: client.get_run(created["id"]), what, run_timeout)
        results = done["results"]
        say(
            f"run {done['id']}: {spec.app}@{spec.version} on {spec.dataset}: "
            f"{sum(1 for r in results if r['passed'])}/{len(results)} cases passed"
        )
        return done

    runs: dict[str, str] = {}
    runs["rag-assistant@v1/rag-support"] = run(RAG_RUN)["id"]
    for spec in BASELINE:
        done = run(spec)
        runs[f"{spec.app}@{spec.version}/{spec.dataset}"] = done["id"]
        client.set_baseline(done["id"], ENVIRONMENT)
        say(f"baseline {spec.app} / {ENVIRONMENT} / {spec.dataset} -> run {done['id']}")

    decisions: dict[str, bool] = {}
    pr4_safety: dict[str, Any] | None = None
    for spec in CANDIDATES:
        done = run(spec)
        key = f"{spec.app}@{spec.version}/{spec.dataset}"
        runs[key] = done["id"]
        policy = project.policies[POLICY[spec.dataset]]
        decision = client.create_release_decision(done["id"], ENVIRONMENT, policy.to_dict())
        passed = sum(1 for c in decision["checks"] if c["passed"])
        decisions[key] = decision["passed"]
        verdict = "PASSED" if decision["passed"] else "FAILED"
        say(f"gate {key} ({POLICY[spec.dataset]}): {verdict}, {passed} of {len(decision['checks'])} checks")
        if spec.version == "pr4-d2-off" and spec.dataset == SAFETY:
            pr4_safety = done

    assert pr4_safety is not None
    result = next((r for r in pr4_safety["results"] if r["case_key"] == REPLAY_CASE), None)
    if result is None:
        raise SeedError(f"PR #4 safety run has no case {REPLAY_CASE}")
    created = client.create_replay(result["id"], {"d2": True})
    replay = _wait(lambda: client.get_replay(created["id"]), f"replay of {REPLAY_CASE}", run_timeout)
    before = "PASS" if result["passed"] else "FAIL"
    after = "PASS" if replay["passed"] else "FAIL"
    say(f"replay {replay['id']}: {REPLAY_CASE} with d2=on: {before} -> {after}")
    return {"runs": runs, "decisions": decisions, "replay_id": replay["id"], "replay_result_id": result["id"]}
