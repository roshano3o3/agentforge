"""`agentforge compare --models ... --datasets ... --repeats N`: one agent, several models.

Every model config runs the same published datasets through the normal
pipeline -- POST /runs, the Docker worker, the deterministic evaluators -- so
each number in the table comes from persisted run results. The CLI only
submits runs, polls them, and summarizes what they stored.

Model specs:
  scripted[:v1|v2]           the invoice agent's scripted planner (no model, no tokens, fixture-based)
  openai:<model>             OpenAI (OPENAI_API_KEY)
  anthropic:<model>          Anthropic (ANTHROPIC_API_KEY)
  ollama:<model>             a local Ollama model (the part after the first ':' is the Ollama name)

A model is skipped, with the reason printed, when its key isn't in the
environment or .env, when Ollama isn't reachable or doesn't have the model,
or when a paid model has no price in the pricing file (its cost couldn't be
estimated or capped).

Cost safety:
  * before anything runs, an estimate per model and in total is printed
    (`estimate`, assumptions stated with it). Over --max-cost it refuses to
    start unless --yes is given;
  * every LLM run carries a cost cap (`max_cost_usd`) equal to what's left of
    --max-cost; the worker stops the run when its estimated spend passes it,
    or when a case's spend can't be measured;
  * after each run the actual spend (reported tokens x the pricing file) is
    added up, and no further run starts once it reaches --max-cost.

LLMs aren't deterministic: with --repeats > 1 each metric is reported as the
mean with the min-max spread over the repeats. The scripted planner is.

A plan file (`--plan`, e.g. benchmarks/compare.yaml) lists the models in run
order, each with its own datasets, repeats and per-case timeout (a slow local
model can run a smaller suite); see `load_plan_file`.

The results are written as JSON to benchmarks/ (schema: benchmarks/schema.json)
with the date, the dataset content hashes, the model ids, the prompt's sha256,
the worker's code version and the pricing file's hash.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from agentforge_evaluators import ModelPrice, cost_usd, percentile

SCHEMA_ID = "agentforge.benchmark/v1"
PROVIDERS = ("openai", "anthropic", "ollama")
KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
SCRIPTED_TARGETS = {"v1": "invoice_agent.adapter:answer_v1", "v2": "invoice_agent.adapter:answer_v2"}
LLM_TARGET = "invoice_agent.adapter:answer_llm"
BENCHMARK_APP = "invoice-agent-benchmark"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"

# The estimate's assumptions (printed with it). Deliberately on the high side.
ASSUMED_TURNS_PER_CASE = 5
ASSUMED_HISTORY_TOKENS_PER_TURN = 300
ASSUMED_OUTPUT_TOKENS_PER_TURN = 400
ASSUMED_REASONING_OUTPUT_TOKENS_PER_TURN = 2_000
# Provider-side tool-use preamble (Anthropic documents 286-497 tokens; OpenAI doesn't publish one).
ASSUMED_TOOL_PREAMBLE_TOKENS = 500
CHARS_PER_TOKEN = 3.0  # conservative (English is usually ~4)
REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4", "claude-opus-5", "claude-sonnet-5", "claude-fable")


class BenchmarkError(Exception):
    pass


@dataclass(frozen=True)
class ModelSpec:
    raw: str
    provider: str  # "scripted" or one of PROVIDERS
    model: str  # for scripted: the preset

    @property
    def is_llm(self) -> bool:
        return self.provider != "scripted"

    @property
    def adapter_target(self) -> str:
        return LLM_TARGET if self.is_llm else SCRIPTED_TARGETS[self.model]

    @property
    def reported_model(self) -> str | None:
        """What the adapter reports as `model` (the pricing key); None for the scripted planner."""
        if not self.is_llm:
            return None
        return f"ollama/{self.model}" if self.provider == "ollama" else self.model

    @property
    def provider_type(self) -> str:
        return f"llm:{self.provider}" if self.is_llm else "local-deterministic"

    def settings(self, temperature: float, prompt: str | None) -> dict[str, Any] | None:
        if not self.is_llm:
            return None
        out: dict[str, Any] = {"provider": self.provider, "model": self.model, "temperature": temperature}
        if prompt is not None:
            out["prompt"] = prompt
        return out


def parse_model_spec(raw: str) -> ModelSpec:
    raw = raw.strip()
    provider, _, model = raw.partition(":")
    if provider == "scripted":
        preset = model or "v1"
        if preset not in SCRIPTED_TARGETS:
            raise BenchmarkError(f"'{raw}': the scripted planner has presets {', '.join(SCRIPTED_TARGETS)}")
        return ModelSpec(raw=f"scripted:{preset}", provider="scripted", model=preset)
    if provider not in PROVIDERS or not model:
        raise BenchmarkError(f"'{raw}': expected scripted[:v1|v2], openai:<model>, anthropic:<model> or ollama:<model>")
    return ModelSpec(raw=raw, provider=provider, model=model)


def split_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass(frozen=True)
class ModelPlan:
    """What one model runs: its datasets (names), repeats, and per-case timeout (None: the CLI's)."""

    datasets: tuple[str, ...]
    repeats: int
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class PlanFile:
    specs: list[ModelSpec]
    entries: dict[str, ModelPlan]  # by spec.raw

    @property
    def dataset_names(self) -> list[str]:
        """Every dataset any model runs, in first-use order."""
        return list(dict.fromkeys(name for e in self.entries.values() for name in e.datasets))


MAX_TIMEOUT_SECONDS = 600.0  # the API's per-case limit
_PLAN_KEYS = {"datasets", "repeats", "models"}
_MODEL_KEYS = {"spec", "datasets", "repeats", "timeout_seconds"}


def load_plan_file(path: Path) -> PlanFile:
    """A comparison plan:

        datasets: [invoice-agent, invoice-agent-safety]   # default for every model
        repeats: 1                                         # default for every model
        models:                                            # run in this order
          - spec: anthropic:claude-haiku-4-5
            repeats: 3
          - spec: ollama:llama3.1:8b
            datasets: [invoice-agent]
            timeout_seconds: 600

    Unknown keys, a repeated spec, or an empty model/dataset list are errors."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BenchmarkError(f"can't read the plan file {path}: {exc}") from None
    if not isinstance(raw, dict) or set(raw) - _PLAN_KEYS:
        raise BenchmarkError(f"{path}: expected a mapping with 'datasets', 'repeats' and 'models' only")
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise BenchmarkError(f"{path}: 'models' must be a non-empty list")
    specs: list[ModelSpec] = []
    entries: dict[str, ModelPlan] = {}
    for i, item in enumerate(models):
        where = f"{path}: models[{i}]"
        if not isinstance(item, dict) or "spec" not in item or set(item) - _MODEL_KEYS:
            raise BenchmarkError(f"{where}: expected 'spec' and optionally 'datasets', 'repeats', 'timeout_seconds'")
        spec = parse_model_spec(str(item["spec"]))
        if spec.raw in entries:
            raise BenchmarkError(f"{where}: '{spec.raw}' is listed twice")
        datasets = item.get("datasets", raw.get("datasets"))
        if not isinstance(datasets, list) or not datasets or not all(isinstance(d, str) and d for d in datasets):
            raise BenchmarkError(f"{where}: 'datasets' must be a non-empty list of dataset names")
        repeats = item.get("repeats", raw.get("repeats", 1))
        if not isinstance(repeats, int) or isinstance(repeats, bool) or not 1 <= repeats <= 20:
            raise BenchmarkError(f"{where}: 'repeats' must be an integer from 1 to 20")
        timeout = item.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, int | float) or isinstance(timeout, bool) or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise BenchmarkError(f"{where}: 'timeout_seconds' must be a number in (0, {MAX_TIMEOUT_SECONDS:g}]")
        specs.append(spec)
        entries[spec.raw] = ModelPlan(tuple(datasets), repeats, float(timeout) if timeout is not None else None)
    return PlanFile(specs, entries)


# -- environment ---------------------------------------------------------------------


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a .env file (comments and blank lines skipped, quotes stripped). Missing: {}."""
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def ollama_models(host: str, timeout: float = 2.0) -> list[str] | None:
    """The models a local Ollama server has, or None if it isn't reachable."""
    try:
        resp = httpx.get(f"{host.rstrip('/')}/api/tags", timeout=timeout)
        resp.raise_for_status()
        return [m.get("name", "") for m in resp.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return None


def skip_reason(
    spec: ModelSpec,
    env: Mapping[str, str],
    pricing: Mapping[str, ModelPrice],
    ollama: Callable[[], list[str] | None],
) -> str | None:
    """Why this model can't run here (None: it can). Keys are only checked for presence, never printed."""
    if not spec.is_llm:
        return None
    if spec.provider in KEY_ENV:
        env_name = KEY_ENV[spec.provider]
        if not env.get(env_name, "").strip():
            return f"{env_name} is not set (environment or .env)"
    else:
        available = ollama()
        if available is None:
            return "Ollama isn't reachable (start it, or set OLLAMA_HOST)"
        names = set(available) | {n.removesuffix(":latest") for n in available}
        if spec.model not in names:
            return f"Ollama doesn't have '{spec.model}' (ollama pull {spec.model})"
    if spec.reported_model not in pricing:
        return f"'{spec.reported_model}' has no price in the pricing file, so its cost can't be estimated or capped"
    return None


# -- estimate ------------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    spec: str
    cases: int
    runs: int
    input_tokens_per_case: int
    output_tokens_per_case: int
    usd: float


def base_input_tokens(prompt_chars: int, tool_schema_chars: int) -> int:
    return int((prompt_chars + tool_schema_chars) / CHARS_PER_TOKEN) + ASSUMED_TOOL_PREAMBLE_TOKENS


def estimate(
    spec: ModelSpec, cases: int, repeats: int, base_tokens: int, pricing: Mapping[str, ModelPrice]
) -> Estimate:
    """A rough, deliberately high estimate: ASSUMED_TURNS_PER_CASE model turns per case, each resending
    the prompt and tool schemas plus the history so far, and a fixed output per turn (more for reasoning
    models, whose thinking is billed as output)."""
    if not spec.is_llm:
        return Estimate(spec.raw, cases, repeats, 0, 0, 0.0)
    turns = ASSUMED_TURNS_PER_CASE
    per_turn_out = (
        ASSUMED_REASONING_OUTPUT_TOKENS_PER_TURN
        if spec.model.startswith(REASONING_PREFIXES)
        else ASSUMED_OUTPUT_TOKENS_PER_TURN
    )
    tokens_in = turns * base_tokens + ASSUMED_HISTORY_TOKENS_PER_TURN * turns * (turns - 1) // 2
    tokens_out = turns * per_turn_out
    price = pricing[spec.reported_model or ""]
    return Estimate(
        spec.raw, cases, repeats, tokens_in, tokens_out, cost_usd(price, tokens_in, tokens_out) * cases * repeats
    )


# -- metrics from a run's stored results ---------------------------------------------

METRICS = (
    "pass_rate",
    "tool_selection",
    "approval_required",
    "injection_direct",
    "injection_indirect",
    "unauthorized_tool",
    "pii_probe",
    "latency_p50_ms",
    "latency_p95_ms",
    "tokens_per_case",
    "cost_per_case_usd",
)
_EVALUATOR_METRICS = ("tool_selection", "approval_required")
_CATEGORY_METRICS = ("injection_direct", "injection_indirect", "unauthorized_tool", "pii_probe")


def _rate(flags: list[bool]) -> float | None:
    return sum(flags) / len(flags) if flags else None


def result_cost(result: Mapping[str, Any], pricing: Mapping[str, ModelPrice]) -> float | None:
    price = pricing.get(result.get("model") or "")
    if price is None or (result.get("input_tokens") is None and result.get("output_tokens") is None):
        return None
    return cost_usd(price, result.get("input_tokens"), result.get("output_tokens"))


def run_metrics(run: Mapping[str, Any], pricing: Mapping[str, ModelPrice]) -> dict[str, float | None]:
    """Each metric over the run's stored results (None: not applicable to this dataset, or no data).
    Rates are case pass rates (evaluator rates: share of cases where it applied and passed)."""
    results = list(run.get("results") or [])
    out: dict[str, float | None] = {"pass_rate": _rate([bool(r["passed"]) for r in results])}
    for name in _EVALUATOR_METRICS:
        verdicts = [
            m["passed"]
            for r in results
            for m in r["metrics"]
            if m["evaluator_name"] == name and m["passed"] is not None
        ]
        out[name] = _rate(verdicts)
    for category in _CATEGORY_METRICS:
        out[category] = _rate(
            [bool(r["passed"]) for r in results if (r.get("safety") or {}).get("category") == category]
        )
    ok_latencies = [r["latency_ms"] for r in results if r["status"] == "ok"]
    out["latency_p50_ms"] = percentile(ok_latencies, 50)
    out["latency_p95_ms"] = percentile(ok_latencies, 95)
    tokens = [
        (r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
        for r in results
        if r.get("input_tokens") is not None or r.get("output_tokens") is not None
    ]
    out["tokens_per_case"] = statistics.fmean(tokens) if tokens else None
    costs = [c for c in (result_cost(r, pricing) for r in results) if c is not None]
    out["cost_per_case_usd"] = statistics.fmean(costs) if costs else None
    return out


def run_spend(run: Mapping[str, Any], pricing: Mapping[str, ModelPrice]) -> float:
    return sum(c for c in (result_cost(r, pricing) for r in run.get("results") or []) if c is not None)


def summarize(values: Iterable[float | None]) -> dict[str, float | int | None]:
    """Mean and min-max over the repeats that produced a value."""
    present = [v for v in values if v is not None]
    if not present:
        return {"mean": None, "min": None, "max": None, "n": 0}
    return {"mean": statistics.fmean(present), "min": min(present), "max": max(present), "n": len(present)}


# -- running ---------------------------------------------------------------------------


class Client(Protocol):
    def upsert_application(self, name: str, description: str | None = None) -> dict[str, Any]: ...
    def upsert_application_version(
        self, application_id: str, version: str, description: str | None = None
    ) -> dict[str, Any]: ...
    def get_dataset_version(self, dataset_name: str, version: int | str = "latest") -> dict[str, Any]: ...
    def create_run(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def get_run(self, run_id: str) -> dict[str, Any]: ...


@dataclass
class RunRecord:
    run_id: str | None
    status: str
    error_message: str | None
    metrics: dict[str, float | None]
    spend_usd: float
    code_version: str | None = None
    pricing_sha256: str | None = None


@dataclass
class Plan:
    specs: list[ModelSpec]
    skipped: dict[str, str]
    datasets: list[dict[str, Any]]
    repeats: int
    estimates: list[Estimate]
    # Per-model datasets / repeats / timeout (from a plan file). A model without an entry runs every
    # dataset `repeats` times with the CLI's timeout.
    entries: dict[str, ModelPlan] = field(default_factory=dict)

    @property
    def runnable(self) -> list[ModelSpec]:
        return [s for s in self.specs if s.raw not in self.skipped]

    def datasets_for(self, spec: ModelSpec) -> list[dict[str, Any]]:
        entry = self.entries.get(spec.raw)
        if entry is None:
            return list(self.datasets)
        by_name = {d["name"]: d for d in self.datasets}
        return [by_name[name] for name in entry.datasets]

    def repeats_for(self, spec: ModelSpec) -> int:
        entry = self.entries.get(spec.raw)
        return entry.repeats if entry is not None else self.repeats

    def timeout_for(self, spec: ModelSpec, default: float) -> float:
        entry = self.entries.get(spec.raw)
        return entry.timeout_seconds if entry is not None and entry.timeout_seconds is not None else default

    @property
    def total_estimate(self) -> float:
        return sum(e.usd for e in self.estimates)


@dataclass
class Outcome:
    runs: dict[tuple[str, str], list[RunRecord]] = field(default_factory=dict)
    spent_usd: float = 0.0
    stopped_reason: str | None = None
    not_run: list[str] = field(default_factory=list)


def wait_for(client: Client, run: dict[str, Any], poll: float, deadline: float, sleep: Callable[[float], None]) -> dict:
    while run["status"] not in ("completed", "failed"):
        if time.monotonic() > deadline:
            raise BenchmarkError(f"run {run['id']} still {run['status']} after the wait timeout")
        sleep(poll)
        run = client.get_run(run["id"])
    return run


def execute(
    client: Client,
    plan: Plan,
    *,
    temperature: float,
    prompt: str | None,
    max_cost: float,
    timeout_seconds: float,
    pricing: Mapping[str, ModelPrice],
    git_commit_sha: str | None,
    log: Callable[[str], None],
    poll: float = 1.0,
    wait_timeout: float = 3600.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Outcome:
    """Run every (model, dataset, repeat), sequentially, within the spend cap."""
    outcome = Outcome()
    app = client.upsert_application(name=BENCHMARK_APP)
    for spec in plan.runnable:
        version = client.upsert_application_version(application_id=app["id"], version=spec.raw)
        repeats = plan.repeats_for(spec)
        for dataset in plan.datasets_for(spec):
            records = outcome.runs.setdefault((spec.raw, dataset["name"]), [])
            for repeat in range(1, repeats + 1):
                label = f"{spec.raw} on {dataset['name']} (repeat {repeat}/{repeats})"
                remaining = max_cost - outcome.spent_usd
                if spec.is_llm and remaining <= 0:
                    outcome.stopped_reason = (
                        f"spend ${outcome.spent_usd:.4f} reached --max-cost ${max_cost:g}; no further runs started"
                    )
                    outcome.not_run.append(label)
                    continue
                payload: dict[str, Any] = {
                    "application_id": app["id"],
                    "application_version_id": version["id"],
                    "dataset_version_id": dataset["id"],
                    "adapter": {"type": "python", "target": spec.adapter_target},
                    "provider_type": spec.provider_type,
                    "timeout_seconds": plan.timeout_for(spec, timeout_seconds) if spec.is_llm else 10.0,
                    "environment": "benchmark",
                    "git_commit_sha": git_commit_sha,
                    "adapter_settings": spec.settings(temperature, prompt),
                    "max_cost_usd": remaining if spec.is_llm else None,
                }
                log(f"running {label}")
                run = client.create_run(payload)
                run = wait_for(client, run, poll, time.monotonic() + wait_timeout, sleep)
                spend = run_spend(run, pricing)
                outcome.spent_usd += spend
                provenance = run.get("provenance") or {}
                records.append(
                    RunRecord(
                        run_id=run["id"],
                        status=run["status"],
                        error_message=run.get("error_message"),
                        metrics=run_metrics(run, pricing),
                        spend_usd=spend,
                        code_version=provenance.get("code_version"),
                        pricing_sha256=provenance.get("pricing_sha256"),
                    )
                )
                log(
                    f"  {run['status']}: {len(run.get('results') or [])} case(s), estimated spend ${spend:.4f} "
                    f"(total ${outcome.spent_usd:.4f} of ${max_cost:g})"
                    + (f" -- {run['error_message']}" if run.get("error_message") else "")
                )
    return outcome


# -- the JSON document -------------------------------------------------------------------


def document(
    plan: Plan,
    outcome: Outcome,
    *,
    created_at: datetime,
    temperature: float,
    prompt_source: str,
    prompt_sha256: str | None,
    max_cost: float,
    git_commit_sha: str | None,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    records = [r for runs in outcome.runs.values() for r in runs]
    results = []
    for (spec_raw, dataset_name), runs in outcome.runs.items():
        results.append(
            {
                "model": spec_raw,
                "dataset": dataset_name,
                "runs": [
                    {
                        "run_id": r.run_id,
                        "status": r.status,
                        "error_message": r.error_message,
                        "spend_usd": r.spend_usd,
                        "metrics": r.metrics,
                    }
                    for r in runs
                ],
                "summary": {m: summarize(r.metrics.get(m) for r in runs if r.status == "completed") for m in METRICS},
            }
        )
    specs = {s.raw: s for s in plan.specs}
    return {
        "schema": SCHEMA_ID,
        "created_at": created_at.isoformat(),
        "date": created_at.date().isoformat(),
        "labels": [
            "synthetic invoice tasks (examples/invoice_agent)",
            "deterministic evaluators -- no LLM judge",
            "cost is estimated: reported tokens x config/pricing.yaml",
        ],
        "agentforge": {
            "git_commit": git_commit_sha,
            "code_versions": sorted({r.code_version for r in records if r.code_version}),
            "pricing_sha256": sorted({r.pricing_sha256 for r in records if r.pricing_sha256}),
        },
        "settings": {
            "repeats": plan.repeats,
            "timeout_seconds": timeout_seconds,
            "temperature": temperature,
            "max_cost_usd": max_cost,
            "prompt": {"source": prompt_source, "sha256": prompt_sha256},
        },
        "datasets": [
            {"name": d["name"], "version": d["version"], "content_hash": d.get("content_hash"), "cases": d["cases"]}
            for d in plan.datasets
        ],
        "models": [
            {
                "spec": raw,
                "provider": specs[raw].provider,
                "model": specs[raw].model,
                "reported_model": specs[raw].reported_model,
                "skipped": plan.skipped.get(raw),
                "datasets": [d["name"] for d in plan.datasets_for(specs[raw])],
                "repeats": plan.repeats_for(specs[raw]),
                "timeout_seconds": plan.timeout_for(specs[raw], timeout_seconds) if specs[raw].is_llm else None,
            }
            for raw in specs
        ],
        "estimate": {
            "assumptions": estimate_assumptions(),
            "per_model": [
                {"model": e.spec, "cases": e.cases, "runs_per_dataset": e.runs, "usd": e.usd} for e in plan.estimates
            ],
            "total_usd": plan.total_estimate,
        },
        "actual_spend_usd": outcome.spent_usd,
        "stopped_reason": outcome.stopped_reason,
        "not_run": outcome.not_run,
        "results": results,
    }


def estimate_assumptions() -> str:
    return (
        f"{ASSUMED_TURNS_PER_CASE} model turns per case; each turn resends the system prompt and tool schemas "
        f"(chars/{CHARS_PER_TOKEN:g} + {ASSUMED_TOOL_PREAMBLE_TOKENS} tool-use tokens) plus "
        f"{ASSUMED_HISTORY_TOKENS_PER_TURN} tokens of history per earlier turn; output "
        f"{ASSUMED_OUTPUT_TOKENS_PER_TURN} tokens per turn ({ASSUMED_REASONING_OUTPUT_TOKENS_PER_TURN} for reasoning "
        "models); standard rates from the pricing file"
    )


def schema() -> dict[str, Any]:
    """The results document's JSON Schema (benchmark_schema.json)."""
    text = resources.files("agentforge_cli").joinpath("benchmark_schema.json").read_text(encoding="utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def validate_document(doc: Mapping[str, Any]) -> None:
    """Raise jsonschema.ValidationError if `doc` doesn't match the schema."""
    from jsonschema import Draft202012Validator

    Draft202012Validator(schema(), format_checker=Draft202012Validator.FORMAT_CHECKER).validate(doc)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_output_path(directory: Path, created_at: datetime, doc: Mapping[str, Any]) -> Path:
    digest = sha256_text(json.dumps(doc, sort_keys=True, default=str))[:8]
    return directory / f"{created_at.strftime('%Y-%m-%d')}-compare-{digest}.json"


def now() -> datetime:
    return datetime.now(UTC)


def environment(env_file: Path) -> dict[str, str]:
    """The process environment over the .env file (the environment wins)."""
    return {**read_env_file(env_file), **os.environ}
