"""Loading and validating dataset YAML files offline (no network call).

    name: <dataset name>
    description: <optional>
    defaults:
      evaluators:                 # version-level default evaluator config
        heuristic_context_precision: {}
        latency: {max_ms: 500}
    test_cases:
      - id: <case key>
        input: <question>
        expected_answer: <optional reference answer>
        expected_context: [<doc id>, ...]
        tags: [...]
        trajectory:               # optional: agent trajectory expectations
          expected_tools: [get_invoice]
          forbidden_tools: [delete_invoice]
        evaluators:               # optional per-case overrides/additions
          answer_contains: {phrases: ["45 days"]}
          heuristic_context_precision: false   # drop a default for this case
        scenario:                 # optional (adversarial): environment setup sent to the adapter
          tool_overrides: {get_customer: {error: {type: TimeoutError, message: "..."}}}
        safety:                   # optional (adversarial): category + expectations, evaluators only
          category: tool_failure
          failing_tool: get_customer

Evaluator configs are validated against the same registry the API uses, so
"valid offline" means the API will accept it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentforge_core.scenario import ScenarioError, validate_scenario
from agentforge_core.schemas import DatasetVersionTestCasesRequest, TestCaseIn
from agentforge_evaluators import (
    EvaluatorConfigError,
    SafetyConfigError,
    TrajectoryConfigError,
    validate_config,
    validate_safety,
    validate_trajectory,
)

_TOP_LEVEL_KEYS = {"name", "description", "defaults", "test_cases"}
_CASE_KEYS = {
    "id",
    "input",
    "expected_answer",
    "expected_context",
    "tags",
    "evaluators",
    "trajectory",
    "scenario",
    "safety",
}


class DatasetFileError(Exception):
    """Raised when a dataset YAML file is missing required structure."""


def load_dataset_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DatasetFileError(f"dataset file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise DatasetFileError(f"{path}: top-level YAML must be a mapping with 'name' and 'test_cases'")
    if "name" not in raw:
        raise DatasetFileError(f"{path}: missing required top-level key 'name'")
    if "test_cases" not in raw:
        raise DatasetFileError(f"{path}: missing required top-level key 'test_cases'")
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise DatasetFileError(f"{path}: unknown top-level key(s) {unknown}")
    return raw


def validate_dataset_file(
    path: Path,
) -> tuple[str, str | None, list[TestCaseIn], dict[str, Any] | None]:
    """Validate a dataset YAML file's structure, test cases and evaluator configs.

    Returns (dataset_name, description, validated_test_cases, default_evaluators).
    Raises DatasetFileError with a human-readable message on any problem.
    """
    raw = load_dataset_file(path)
    name = raw["name"]
    description = raw.get("description")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict) or set(defaults) - {"evaluators"}:
        raise DatasetFileError(f"{path}: 'defaults' may only contain 'evaluators'")
    try:
        default_evaluators = validate_config(defaults.get("evaluators"), allow_disable=False)
    except EvaluatorConfigError as exc:
        raise DatasetFileError(f"{path}: defaults.evaluators: {exc}") from exc

    raw_cases = raw["test_cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetFileError(f"{path}: 'test_cases' must be a non-empty list")

    test_cases: list[TestCaseIn] = []
    for i, case in enumerate(raw_cases):
        if not isinstance(case, dict):
            raise DatasetFileError(f"{path}: test_cases[{i}] must be a mapping")
        if "id" not in case:
            raise DatasetFileError(f"{path}: test_cases[{i}] is missing required key 'id'")
        unknown = sorted(set(case) - _CASE_KEYS)
        if unknown:
            raise DatasetFileError(f"{path}: test_cases[{i}] ('{case['id']}') has unknown key(s) {unknown}")
        try:
            evaluators = validate_config(case.get("evaluators"))
        except EvaluatorConfigError as exc:
            raise DatasetFileError(f"{path}: test_cases[{i}] ('{case['id']}') evaluators: {exc}") from exc
        try:
            trajectory = validate_trajectory(case.get("trajectory"))
        except TrajectoryConfigError as exc:
            raise DatasetFileError(f"{path}: test_cases[{i}] ('{case['id']}') trajectory: {exc}") from exc
        try:
            scenario = validate_scenario(case.get("scenario"))
        except ScenarioError as exc:
            raise DatasetFileError(f"{path}: test_cases[{i}] ('{case['id']}') scenario: {exc}") from exc
        try:
            safety = validate_safety(case.get("safety"))
        except SafetyConfigError as exc:
            raise DatasetFileError(f"{path}: test_cases[{i}] ('{case['id']}') safety: {exc}") from exc
        try:
            test_cases.append(
                TestCaseIn(
                    case_key=case["id"],
                    input=case.get("input", ""),
                    expected_answer=case.get("expected_answer"),
                    expected_context=case.get("expected_context") or [],
                    tags=case.get("tags") or [],
                    evaluators=evaluators,
                    trajectory=trajectory,
                    scenario=scenario,
                    safety=safety,
                )
            )
        except ValidationError as exc:
            raise DatasetFileError(f"{path}: test_cases[{i}] ('{case.get('id')}') failed validation: {exc}") from exc

    # Cross-case validation (duplicate case_key, etc.) via the same request
    # schema the API uses, so "valid offline" really means "the API will
    # accept this".
    try:
        DatasetVersionTestCasesRequest(test_cases=test_cases, default_evaluators=default_evaluators)
    except ValidationError as exc:
        raise DatasetFileError(f"{path}: {exc}") from exc

    return name, description, test_cases, default_evaluators
