"""`agentforge demo seed` against a fake API client: what it publishes, runs, gates and replays, in which order.

The real thing (API + Docker worker, fresh database) was run by hand on a fresh clone: see docs/results.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentforge_cli import demo

ROOT = Path(__file__).parents[2]


class FakeClient:
    """Answers like the API; every run 'completes' at once with one passing and one failing case."""

    def __init__(self, applications: list[Any] | None = None, runs: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._applications = applications or []
        self._runs_listed = runs or []
        self._runs: dict[str, dict[str, Any]] = {}
        self._run_specs: dict[str, dict[str, Any]] = {}

    def list_applications(self) -> list[Any]:
        return self._applications

    def list_runs(self) -> list[Any]:
        return self._runs_listed

    def upsert_dataset(self, name: str, description: str | None = None) -> dict[str, Any]:
        return {"id": f"ds-{name}"}

    def create_draft_version(self, dataset_id: str, test_cases: list[Any], default_evaluators: Any) -> dict[str, Any]:
        self.calls.append(("draft", (dataset_id, len(test_cases))))
        return {"version": 1}

    def publish_dataset_version(self, dataset_name: str, version: int) -> dict[str, Any]:
        self.calls.append(("publish", dataset_name))
        return {"version": version, "content_hash": "sha256:x"}

    def upsert_application(self, name: str) -> dict[str, Any]:
        return {"id": f"app-{name}"}

    def upsert_application_version(self, application_id: str, version: str) -> dict[str, Any]:
        return {"id": f"{application_id}-{version}"}

    def get_dataset_version(self, name: str, version: str) -> dict[str, Any]:
        return {"id": f"dv-{name}"}

    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        run_id = f"run-{len(self._runs) + 1}"
        self.calls.append(
            ("run", (payload["application_version_id"], payload["dataset_version_id"], payload["adapter"]))
        )
        self._run_specs[run_id] = payload
        self._runs[run_id] = {
            "id": run_id,
            "status": "completed",
            "results": [
                {"id": f"{run_id}-r1", "case_key": demo.REPLAY_CASE, "passed": False},
                {"id": f"{run_id}-r2", "case_key": "other", "passed": True},
            ],
        }
        return {"id": run_id, "status": "pending"}

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._runs[run_id]

    def set_baseline(self, run_id: str, environment: str) -> dict[str, Any]:
        self.calls.append(("baseline", (run_id, environment)))
        return {}

    def create_release_decision(self, candidate: str, baseline: str, policy: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("gate", (candidate, baseline, sorted(policy))))
        return {"passed": False, "checks": [{"passed": True}, {"passed": False}]}

    def create_replay(self, result_id: str, overrides: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("replay", (result_id, overrides)))
        return {"id": "replay-1", "status": "pending"}

    def get_replay(self, replay_id: str) -> dict[str, Any]:
        return {"id": replay_id, "status": "completed", "passed": True}


def test_seed_publishes_runs_gates_and_replays_in_order() -> None:
    client = FakeClient()
    lines: list[str] = []
    out = demo.seed(client, ROOT, lines.append)  # type: ignore[arg-type]

    assert [c[1] for c in client.calls if c[0] == "publish"] == ["rag-support", "invoice-agent", "invoice-agent-safety"]
    runs = [c[1] for c in client.calls if c[0] == "run"]
    assert [(v, d, a["target"]) for v, d, a in runs] == [
        ("app-rag-assistant-v1", "dv-rag-support", "rag_app.adapter:answer"),
        ("app-invoice-agent-v1", "dv-invoice-agent", "invoice_agent.adapter:answer_v1"),
        ("app-invoice-agent-v1", "dv-invoice-agent-safety", "invoice_agent.adapter:answer_v1"),
        ("app-invoice-agent-v2", "dv-invoice-agent", "invoice_agent.adapter:answer_v2"),
        ("app-invoice-agent-v2", "dv-invoice-agent-safety", "invoice_agent.adapter:answer_v2"),
        ("app-invoice-agent-pr4-d2-off", "dv-invoice-agent", "invoice_agent.adapter:answer_v1_without_d2"),
        ("app-invoice-agent-pr4-d2-off", "dv-invoice-agent-safety", "invoice_agent.adapter:answer_v1_without_d2"),
    ]
    # v1 is the production baseline for both invoice datasets; every candidate is gated against it.
    assert [c[1] for c in client.calls if c[0] == "baseline"] == [("run-2", "production"), ("run-3", "production")]
    gates = [c[1] for c in client.calls if c[0] == "gate"]
    assert [g[0] for g in gates] == ["run-4", "run-5", "run-6", "run-7"]
    assert all(g[1] == "production" for g in gates)
    # One replay: PR #4's failing injection case on the safety dataset, with D2 on.
    assert [c[1] for c in client.calls if c[0] == "replay"] == [("run-7-r1", {"d2": True})]
    assert out["replay_id"] == "replay-1"
    assert any("FAIL -> PASS" in line for line in lines)


def test_gates_use_the_repos_policies() -> None:
    client = FakeClient()
    demo.seed(client, ROOT, lambda _line: None)  # type: ignore[arg-type]
    gates = [c[1] for c in client.calls if c[0] == "gate"]
    trajectory, safety = gates[0][2], gates[1][2]
    assert trajectory == ["cases", "maximums", "minimums", "regressions"]  # release_policy
    assert "cases" not in safety  # safety_policy has no case rule


@pytest.mark.parametrize(
    ("applications", "runs", "empty"),
    [([], [], True), ([{"name": "x"}], [], False), ([], [{"id": "r"}], False)],
)
def test_only_an_empty_database_is_seeded(applications: list[Any], runs: list[Any], empty: bool) -> None:
    assert demo.is_empty(FakeClient(applications, runs)) is empty  # type: ignore[arg-type]


def test_a_failed_run_stops_the_seed() -> None:
    client = FakeClient()
    client.get_run = lambda run_id: {"id": run_id, "status": "failed", "error_message": "worker error: boom"}  # type: ignore[method-assign]
    with pytest.raises(demo.SeedError, match="worker error: boom"):
        demo.seed(client, ROOT, lambda _line: None)  # type: ignore[arg-type]
