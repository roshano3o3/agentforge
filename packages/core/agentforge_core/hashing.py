"""Content hash of a dataset version: what a run, a benchmark or a generated
dataset cites to say exactly which test cases it used.

    sha256 over canonical JSON of {default_evaluators, test_cases}

Test cases are sorted by case_key and reduced to their content fields;
keys are sorted, no whitespace, UTF-8. The two Phase 2 legacy answer
fields are included only when set, so a version written before they were
retired hashes the same way it always would have, and a new version (which
can't set them) hashes the same from its YAML file as from the API.

Published versions are frozen, so their hash never changes; the API reports
none for a draft, whose content can still change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

HASH_PREFIX = "sha256:"

CONTENT_FIELDS = (
    "case_key",
    "input",
    "expected_answer",
    "expected_context",
    "tags",
    "evaluators",
    "trajectory",
    "scenario",
    "safety",
)
_LEGACY_FIELDS = ("expected_answer_contains", "expected_answer_regex")


def _case(case: Mapping[str, Any]) -> dict[str, Any]:
    out = {name: case.get(name) for name in CONTENT_FIELDS}
    out["expected_context"] = list(out["expected_context"] or [])
    out["tags"] = list(out["tags"] or [])
    for name in _LEGACY_FIELDS:
        if case.get(name):
            out[name] = case[name]
    return out


def dataset_content_hash(default_evaluators: Mapping[str, Any] | None, test_cases: Iterable[Mapping[str, Any]]) -> str:
    cases = sorted((_case(c) for c in test_cases), key=lambda c: c["case_key"])
    payload = {"default_evaluators": default_evaluators, "test_cases": cases}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return HASH_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
