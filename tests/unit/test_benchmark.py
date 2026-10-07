"""`agentforge compare --models`: specs, skip rules, the estimate, the spend cap, the results JSON.

The API is a fake here (FakeClient): it records the runs the CLI submits and
answers with stored-run JSON built by the test. The real pipeline (API ->
worker -> evaluators) for the LLM adapter is covered in
tests/integration/test_llm_runs.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from typer.testing import CliRunner

from agentforge_cli import benchmark, main
from agentforge_evaluators import ModelPrice

PRICES = {
    "claude-haiku-4-5": ModelPrice(1.0, 5.0),
    "gpt-5.4-mini": ModelPrice(0.75, 4.5),
    "ollama/llama3.1:8b": ModelPrice(0.0, 0.0),
}
DATASET = {"id": "dv1", "name": "invoice-agent", "version": 1, "content_hash": "sha256:" + "a" * 64, "cases": 2}


def _result(passed: bool, *, model: str | None, tokens=(1000, 200), category=None, metrics=(), latency=50.0) -> dict:
    return {
        "passed": passed,
        "status": "ok",
        "latency_ms": latency,
        "model": model,
        "input_tokens": tokens[0] if tokens else None,
        "output_tokens": tokens[1] if tokens else None,
        "safety": {"category": category} if category else None,
        "metrics": [{"evaluator_name": n, "passed": p} for n, p in metrics],
    }


class FakeClient:
    """Answers create_run with the next prepared run (completed at once)."""

    def __init__(self, runs: list[dict]) -> None:
        self.prepared = list(runs)
        self.created: list[dict] = []

    def upsert_application(self, name: str, description: str | None = None) -> dict:
        return {"id": "app1", "name": name}

    def upsert_application_version(self, application_id: str, version: str, description: str | None = None) -> dict:
        return {"id": f"ver-{version}"}

    def get_dataset_version(self, dataset_name: str, version: int | str = "latest") -> dict:
        return {"id": "dv1", "version": 1, "content_hash": DATASET["content_hash"], "test_cases": [{}, {}]}

    def create_run(self, payload: dict) -> dict:
        self.created.append(payload)
        run = self.prepared.pop(0)
        return {"id": f"run{len(self.created)}", "status": "completed", "error_message": None, **run}

    def get_run(self, run_id: str) -> dict:  # never needed: runs come back finished
        raise AssertionError("unexpected poll")


# -- specs and skipping ------------------------------------------------------------------


def test_model_specs() -> None:
    assert benchmark.parse_model_spec("scripted") == benchmark.ModelSpec("scripted:v1", "scripted", "v1")
    ollama = benchmark.parse_model_spec("ollama:llama3.1:8b")
    assert (ollama.provider, ollama.model, ollama.reported_model) == ("ollama", "llama3.1:8b", "ollama/llama3.1:8b")
    claude = benchmark.parse_model_spec("anthropic:claude-haiku-4-5")
    assert claude.adapter_target == "invoice_agent.adapter:answer_llm" and claude.provider_type == "llm:anthropic"
    assert claude.settings(0.0, None) == {"provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.0}
    assert benchmark.parse_model_spec("scripted:v2").settings(0.0, "p") is None  # the scripted planner takes none
    for bad in ("openai", "bedrock:x", "scripted:v9", ""):
        with pytest.raises(benchmark.BenchmarkError):
            benchmark.parse_model_spec(bad)


def test_models_are_skipped_with_a_reason() -> None:
    no_ollama = lambda: None  # noqa: E731
    skip = benchmark.skip_reason
    spec = benchmark.parse_model_spec
    assert skip(spec("openai:gpt-5.4-mini"), {}, PRICES, no_ollama) == "OPENAI_API_KEY is not set (environment or .env)"
    assert skip(spec("anthropic:claude-haiku-4-5"), {"ANTHROPIC_API_KEY": "  "}, PRICES, no_ollama) is not None
    assert skip(spec("anthropic:claude-haiku-4-5"), {"ANTHROPIC_API_KEY": "k"}, PRICES, no_ollama) is None
    assert "Ollama isn't reachable" in skip(spec("ollama:llama3.1:8b"), {}, PRICES, no_ollama)
    assert "ollama pull llama3.1:8b" in skip(spec("ollama:llama3.1:8b"), {}, PRICES, lambda: ["qwen2.5:7b"])
    assert skip(spec("ollama:llama3.1:8b"), {}, PRICES, lambda: ["llama3.1:8b"]) is None
    unpriced = skip(spec("openai:gpt-9"), {"OPENAI_API_KEY": "k"}, PRICES, no_ollama)
    assert unpriced == "'gpt-9' has no price in the pricing file, so its cost can't be estimated or capped"
    assert skip(spec("scripted"), {}, {}, no_ollama) is None


def test_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nOPENAI_API_KEY='abc'\nEMPTY=\nPOSTGRES_DB=agentforge\n", encoding="utf-8")
    assert benchmark.read_env_file(env) == {"OPENAI_API_KEY": "abc", "EMPTY": "", "POSTGRES_DB": "agentforge"}
    assert benchmark.read_env_file(tmp_path / "missing") == {}


# -- the estimate --------------------------------------------------------------------------


def test_estimate_arithmetic() -> None:
    base = benchmark.base_input_tokens(prompt_chars=3000, tool_schema_chars=3000)
    assert base == 2000 + benchmark.ASSUMED_TOOL_PREAMBLE_TOKENS
    e = benchmark.estimate(benchmark.parse_model_spec("anthropic:claude-haiku-4-5"), 10, 3, base, PRICES)
    turns = benchmark.ASSUMED_TURNS_PER_CASE
    tokens_in = turns * base + benchmark.ASSUMED_HISTORY_TOKENS_PER_TURN * turns * (turns - 1) // 2
    tokens_out = turns * benchmark.ASSUMED_OUTPUT_TOKENS_PER_TURN
    assert (e.input_tokens_per_case, e.output_tokens_per_case) == (tokens_in, tokens_out)
    assert e.usd == pytest.approx((tokens_in * 1.0 + tokens_out * 5.0) / 1e6 * 10 * 3)
    # Reasoning models are assumed to write far more (thinking is billed as output).
    r = benchmark.estimate(benchmark.parse_model_spec("openai:gpt-5.4-mini"), 10, 1, base, PRICES)
    assert r.output_tokens_per_case == turns * benchmark.ASSUMED_REASONING_OUTPUT_TOKENS_PER_TURN
    assert benchmark.estimate(benchmark.parse_model_spec("scripted"), 10, 3, base, PRICES).usd == 0


# -- running, the cap, repeats ----------------------------------------------------------------


def _plan(specs: list[str], repeats: int = 1, est: float = 0.01) -> benchmark.Plan:
    parsed = [benchmark.parse_model_spec(s) for s in specs]
    return benchmark.Plan(
        parsed, {}, [DATASET], repeats, [benchmark.Estimate(s.raw, 2, repeats, 1, 1, est) for s in parsed]
    )


def _execute(client: FakeClient, plan: benchmark.Plan, max_cost: float = 5.0) -> benchmark.Outcome:
    return benchmark.execute(
        client, plan, temperature=0.0, prompt="PROMPT", max_cost=max_cost, timeout_seconds=90, pricing=PRICES,
        git_commit_sha="abc", log=lambda _line: None,
    )  # fmt: skip


def test_runs_carry_the_model_settings_and_the_remaining_budget() -> None:
    run = {"results": [_result(True, model="claude-haiku-4-5"), _result(False, model="claude-haiku-4-5")]}
    client = FakeClient([run, {"results": [_result(True, model=None, tokens=None)] * 2}])
    outcome = _execute(client, _plan(["anthropic:claude-haiku-4-5", "scripted"]))
    llm_run, scripted_run = client.created
    assert llm_run["adapter"] == {"type": "python", "target": "invoice_agent.adapter:answer_llm"}
    assert llm_run["adapter_settings"] == {
        "provider": "anthropic", "model": "claude-haiku-4-5", "temperature": 0.0, "prompt": "PROMPT",
    }  # fmt: skip
    assert llm_run["max_cost_usd"] == 5.0 and llm_run["timeout_seconds"] == 90
    assert llm_run["provider_type"] == "llm:anthropic"
    assert scripted_run["adapter_settings"] is None and scripted_run["max_cost_usd"] is None
    assert scripted_run["provider_type"] == "local-deterministic"
    per_case = (1000 * 1.0 + 200 * 5.0) / 1e6
    assert outcome.spent_usd == pytest.approx(2 * per_case)


def test_no_run_starts_once_spend_reaches_the_cap() -> None:
    expensive = {"results": [_result(True, model="claude-haiku-4-5", tokens=(1_000_000, 0))]}  # $1.00
    client = FakeClient([expensive, expensive, expensive])
    outcome = _execute(client, _plan(["anthropic:claude-haiku-4-5"], repeats=3), max_cost=1.5)
    assert len(client.created) == 2
    assert client.created[1]["max_cost_usd"] == pytest.approx(0.5)  # what's left goes to the worker as the run's cap
    assert outcome.spent_usd == pytest.approx(2.0)
    assert outcome.stopped_reason == "spend $2.0000 reached --max-cost $1.5; no further runs started"
    assert outcome.not_run == ["anthropic:claude-haiku-4-5 on invoice-agent (repeat 3/3)"]


def test_metrics_and_spread_over_repeats() -> None:
    def run(pass_first: bool, latency: float) -> dict:
        return {
            "results": [
                _result(pass_first, model="gpt-5.4-mini", metrics=[("tool_selection", pass_first)], latency=latency),
                _result(True, model="gpt-5.4-mini", metrics=[("tool_selection", True), ("approval_required", True)],
                        category="injection_direct", latency=latency * 2),
            ]
        }  # fmt: skip

    client = FakeClient([run(True, 100.0), run(False, 300.0)])
    plan = _plan(["openai:gpt-5.4-mini"], repeats=2)
    outcome = _execute(client, plan)
    doc = benchmark.document(
        plan, outcome, created_at=benchmark.now(), temperature=0.0, prompt_source="built-in",
        prompt_sha256="f" * 64, max_cost=5.0, git_commit_sha="abc",
    )  # fmt: skip
    benchmark.validate_document(doc)
    row = doc["results"][0]
    assert [r["metrics"]["pass_rate"] for r in row["runs"]] == [1.0, 0.5]
    assert row["summary"]["pass_rate"] == {"mean": 0.75, "min": 0.5, "max": 1.0, "n": 2}
    assert row["summary"]["tool_selection"]["mean"] == 0.75
    assert row["summary"]["approval_required"] == {"mean": 1.0, "min": 1.0, "max": 1.0, "n": 2}
    assert row["summary"]["injection_direct"]["mean"] == 1.0
    assert row["summary"]["pii_probe"] == {"mean": None, "min": None, "max": None, "n": 0}  # not in this dataset
    assert row["summary"]["latency_p95_ms"] == {"mean": 400.0, "min": 200.0, "max": 600.0, "n": 2}
    assert row["summary"]["tokens_per_case"]["mean"] == 1200
    assert row["summary"]["cost_per_case_usd"]["mean"] == pytest.approx((1000 * 0.75 + 200 * 4.5) / 1e6)
    assert doc["datasets"] == [{k: DATASET[k] for k in ("name", "version", "content_hash", "cases")}]
    assert doc["settings"]["prompt"] == {"source": "built-in", "sha256": "f" * 64}


def test_the_schema_rejects_a_malformed_document() -> None:
    plan = _plan(["scripted"])
    doc = benchmark.document(
        plan, benchmark.Outcome(), created_at=benchmark.now(), temperature=0.0, prompt_source="built-in",
        prompt_sha256=None, max_cost=5.0, git_commit_sha=None,
    )  # fmt: skip
    benchmark.validate_document(doc)
    for broken in ({**doc, "schema": "v0"}, {**doc, "extra": 1}, {k: v for k, v in doc.items() if k != "datasets"}):
        with pytest.raises(jsonschema.ValidationError):
            benchmark.validate_document(broken)


# -- the CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch) -> tuple[Path, list[FakeClient]]:
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OLLAMA_HOST"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(benchmark, "ollama_models", lambda host, timeout=2.0: None)
    clients: list[FakeClient] = []

    class Ctx(FakeClient):
        def __init__(self, base_url: str) -> None:
            super().__init__([{"results": [_result(True, model="claude-haiku-4-5")] * 2}] * 4)
            clients.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            pass

    monkeypatch.setattr(main, "AgentForgeClient", Ctx)
    return tmp_path, clients


def _plain(res) -> str:
    """The CLI's output without ANSI styling: Rich colors it on GitHub Actions even through a pipe."""
    return re.sub(r"\x1b\[[0-9;]*m", "", res.output)


def _compare(tmp_path: Path, *args: str):
    return CliRunner().invoke(
        main.app,
        ["compare", "--env-file", str(tmp_path / ".env"), "--output", str(tmp_path / "out.json"), *args],
    )


def test_cli_skips_models_without_keys_and_runs_nothing(cli_env) -> None:
    tmp_path, clients = cli_env
    res = _compare(tmp_path, "--models", "openai:gpt-5.4-mini,ollama:llama3.1:8b")
    assert res.exit_code == 1, res.output
    assert "Skipping openai:gpt-5.4-mini: OPENAI_API_KEY is not set" in _plain(res)
    assert "Skipping ollama:llama3.1:8b: Ollama isn't reachable" in _plain(res)
    assert "No model can run here; nothing was started." in _plain(res)
    assert all(c.created == [] for c in clients)


def test_cli_refuses_an_estimate_over_the_cap_unless_yes(cli_env) -> None:
    tmp_path, clients = cli_env
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=fake-key-for-tests\n", encoding="utf-8")
    args = ("--models", "anthropic:claude-haiku-4-5", "--datasets", "invoice-agent", "--repeats", "2")
    res = _compare(tmp_path, *args, "--max-cost", "0.0001")
    assert res.exit_code == 1, res.output
    assert "Refusing to start: the estimate" in _plain(res) and clients[-1].created == []
    assert "fake-key-for-tests" not in _plain(res)

    res = _compare(tmp_path, *args, "--max-cost", "0.0001", "--yes")
    assert res.exit_code == 0, res.output
    created = clients[-1].created
    assert len(created) == 1  # the first run's spend passed the cap: the second repeat never started
    assert created[0]["max_cost_usd"] == pytest.approx(0.0001)
    doc = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    benchmark.validate_document(doc)
    assert doc["stopped_reason"].startswith("spend $") and len(doc["not_run"]) == 1
    assert "fake-key-for-tests" not in (tmp_path / "out.json").read_text(encoding="utf-8")


def test_cli_scripted_baseline_runs_without_any_key(cli_env) -> None:
    tmp_path, clients = cli_env
    res = _compare(tmp_path, "--models", "scripted", "--datasets", "invoice-agent")
    assert res.exit_code == 0, res.output
    assert clients[-1].created[0]["adapter"]["target"] == "invoice_agent.adapter:answer_v1"
    doc: dict[str, Any] = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert doc["models"][0]["provider"] == "scripted" and doc["estimate"]["total_usd"] == 0
    assert doc["settings"]["prompt"]["sha256"] is not None


def test_compare_still_does_run_vs_run() -> None:
    res = CliRunner().invoke(main.app, ["compare"])
    assert res.exit_code != 0 and "--baseline and --candidate" in _plain(res)
