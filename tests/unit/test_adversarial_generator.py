"""The adversarial generator: deterministic, every variant traceable to its
category and source case, and the committed safety dataset is exactly what
the generator produces from the committed base dataset and profile."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agentforge_cli import adversarial
from agentforge_cli.dataset_io import validate_dataset_file
from agentforge_core.hashing import dataset_content_hash

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "datasets" / "invoice_agent_v1.yaml"
PROFILE = ROOT / "examples" / "invoice_agent" / "adversarial_profile.yaml"
COMMITTED = ROOT / "datasets" / "invoice_agent_safety_v1.yaml"
PROFILE_LABEL = "examples/invoice_agent/adversarial_profile.yaml"


def _base() -> tuple[str, list[dict[str, Any]]]:
    _name, _desc, cases, defaults = validate_dataset_file(BASE)
    dumped = [c.model_dump() for c in cases]
    return dataset_content_hash(defaults, dumped), dumped


def _profile() -> dict[str, Any]:
    return yaml.safe_load(PROFILE.read_text(encoding="utf-8"))


def _generate(seed: int = 7, per_category: int = 5) -> adversarial.GeneratedDataset:
    base_hash, cases = _base()
    return adversarial.generate(
        "invoice-agent",
        base_hash,
        cases,
        _profile(),
        seed=seed,
        per_category=per_category,
        name="invoice-agent-safety",
        profile_label=PROFILE_LABEL,
    )


def test_same_base_profile_and_seed_give_identical_output() -> None:
    assert _generate().to_yaml() == _generate().to_yaml()


def test_a_different_seed_gives_a_different_selection() -> None:
    assert [c["id"] for c in _generate(seed=7).test_cases] != [c["id"] for c in _generate(seed=8).test_cases]


def test_committed_safety_dataset_is_exactly_the_generator_output() -> None:
    # Regenerate with: agentforge adversarial generate --dataset invoice-agent --seed 7 --per-category 5
    #   --profile examples/invoice_agent/adversarial_profile.yaml --out datasets/invoice_agent_safety_v1.yaml
    committed = COMMITTED.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert committed == _generate().to_yaml()


def test_every_variant_records_its_category_and_source_case() -> None:
    generated = _generate()
    base_keys = {c["case_key"] for c in _base()[1]}
    assert len(generated.test_cases) == 35
    by_category: dict[str, int] = {}
    for case in generated.test_cases:
        safety = case["safety"]
        assert safety["category"] in adversarial.CATEGORIES
        assert safety["source_case"] in base_keys
        assert case["id"] == f"{safety['category']}.{safety['technique']}.{safety['source_case']}"
        assert case["tags"][:2] == ["adversarial", safety["category"]]
        by_category[safety["category"]] = by_category.get(safety["category"], 0) + 1
    assert by_category == {category: 5 for category in adversarial.CATEGORIES}


def test_attack_metadata_never_goes_in_the_scenario_sent_to_the_adapter() -> None:
    for case in _generate(per_category=1000).test_cases:
        scenario = case.get("scenario") or {}
        assert set(scenario) <= {"tool_overrides", "user"}
        text = str(scenario)
        assert case["safety"]["category"] not in text
        assert "source_case" not in text and "forbidden" not in text


def test_variants_keep_the_base_cases_safety_rules() -> None:
    base = {c["case_key"]: c for c in _base()[1]}
    for case in _generate().test_cases:
        source = base[case["safety"]["source_case"]]
        rules = {
            k: v
            for k, v in (source["trajectory"] or {}).items()
            if k in ("forbidden_tools", "requires_approval_before")
        }
        if case["safety"]["category"] == "pii_probe":
            continue  # a probe is a new request; it forbids every write tool instead
        for key, value in rules.items():
            assert case["trajectory"][key] == value, (case["id"], key)


def test_generated_file_validates_and_its_hash_is_stable(tmp_path: Path) -> None:
    out = tmp_path / "safety.yaml"
    out.write_bytes(_generate().to_yaml().encode("utf-8"))
    _name, _desc, cases, defaults = validate_dataset_file(out)
    assert len(cases) == 35
    first = dataset_content_hash(defaults, [c.model_dump() for c in cases])
    assert first == dataset_content_hash(defaults, [c.model_dump() for c in reversed(cases)])
    assert first.startswith("sha256:")


def test_profile_validation_names_the_problem() -> None:
    profile = _profile()
    del profile["secrets"]
    with pytest.raises(adversarial.ProfileError, match="missing \\['secrets'\\]"):
        adversarial.validate_profile(profile)
