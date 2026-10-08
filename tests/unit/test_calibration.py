"""The answer-check calibration set (datasets/calibration/answer_checks_v1.yaml).

Its labels were written by hand before any evaluator was changed and are frozen: the hash below pins
every (id, check, source result, label). Changing a label means a new calibration file, not an edit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

CALIBRATION = Path(__file__).parents[2] / "datasets" / "calibration" / "answer_checks_v1.yaml"
FROZEN_LABELS_SHA256 = "27d8ab81ac08017d0a7a0a8d20780515fcc04dce239c8eada9d4b3cde147d154"


def load_items() -> list[dict]:
    items: list[dict] = yaml.safe_load(CALIBRATION.read_text(encoding="utf-8"))["items"]
    return items


def test_labels_are_frozen() -> None:
    items = load_items()
    canon = json.dumps(
        [[i["id"], i["check"], i["source"]["result_id"], i["label"]] for i in items], separators=(",", ":")
    )
    assert hashlib.sha256(canon.encode()).hexdigest() == FROZEN_LABELS_SHA256


def test_every_item_is_complete() -> None:
    items = load_items()
    assert len(items) == 45 and len({i["id"] for i in items}) == 45
    for item in items:
        assert item["check"] in ("answer_contains", "answer_regex", "graceful_tool_failure")
        assert item["label"] in ("correct", "incorrect") and item["reason"].strip()
        assert item["answer"] and item["steps"][-1]["kind"] == "final_answer"
        if item["check"] != "graceful_tool_failure":
            assert item["params"], item["id"]
