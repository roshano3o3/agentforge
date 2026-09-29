"""Loading and validating dataset YAML files offline (no network call)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentforge_core.schemas import DatasetVersionPublishRequest, TestCaseIn


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
    return raw


def validate_dataset_file(path: Path) -> tuple[str, str | None, list[TestCaseIn]]:
    """Validate a dataset YAML file's structure and test-case schema.

    Returns (dataset_name, description, validated_test_cases). Raises
    DatasetFileError or pydantic.ValidationError with a human-readable
    message on any problem.
    """
    raw = load_dataset_file(path)
    name = raw["name"]
    description = raw.get("description")

    raw_cases = raw["test_cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetFileError(f"{path}: 'test_cases' must be a non-empty list")

    test_cases: list[TestCaseIn] = []
    for i, case in enumerate(raw_cases):
        if not isinstance(case, dict):
            raise DatasetFileError(f"{path}: test_cases[{i}] must be a mapping")
        if "id" not in case:
            raise DatasetFileError(f"{path}: test_cases[{i}] is missing required key 'id'")
        try:
            test_cases.append(
                TestCaseIn(
                    case_key=case["id"],
                    input=case.get("input", ""),
                    expected_answer=case.get("expected_answer"),
                    expected_context=case.get("expected_context") or [],
                    tags=case.get("tags") or [],
                )
            )
        except ValidationError as exc:
            raise DatasetFileError(f"{path}: test_cases[{i}] ('{case.get('id')}') failed validation: {exc}") from exc

    # Cross-case validation (duplicate case_key, etc.) via the same request
    # schema the API uses, so "valid offline" really means "the API will
    # accept this".
    try:
        DatasetVersionPublishRequest(test_cases=test_cases)
    except ValidationError as exc:
        raise DatasetFileError(f"{path}: {exc}") from exc

    return name, description, test_cases
