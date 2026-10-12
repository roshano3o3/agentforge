"""The answer-check calibration sets (datasets/calibration/).

answer_checks_v1.yaml is the dev set: its labels were written by hand before any evaluator was changed.
answer_checks_holdout_v1.yaml is held out: outputs not in the dev set, labeled before any evaluator was run
on them. Both are frozen: a hash pins every (id, check, source result, label). Changing a label means a new
calibration file, not an edit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from agentforge_cli import mutants

CALIBRATION_DIR = Path(__file__).parents[2] / "datasets" / "calibration"
CALIBRATION = CALIBRATION_DIR / "answer_checks_v1.yaml"
HOLDOUT = CALIBRATION_DIR / "answer_checks_holdout_v1.yaml"
FROZEN_LABELS_SHA256 = "27d8ab81ac08017d0a7a0a8d20780515fcc04dce239c8eada9d4b3cde147d154"
FROZEN_HOLDOUT_LABELS_SHA256 = "c1c915cc487321c1dbef6f57b6622ad2b0e945f176188e12ef158f3e9da84b60"

SETS = [(CALIBRATION, FROZEN_LABELS_SHA256, 45), (HOLDOUT, FROZEN_HOLDOUT_LABELS_SHA256, 25)]


def load_items(path: Path = CALIBRATION) -> list[dict]:
    items: list[dict] = yaml.safe_load(path.read_text(encoding="utf-8"))["items"]
    return items


@pytest.mark.parametrize(("path", "sha256", "size"), SETS, ids=["dev", "holdout"])
def test_labels_are_frozen(path: Path, sha256: str, size: int) -> None:
    items = load_items(path)
    canon = json.dumps(
        [[i["id"], i["check"], i["source"]["result_id"], i["label"]] for i in items], separators=(",", ":")
    )
    assert hashlib.sha256(canon.encode()).hexdigest() == sha256


@pytest.mark.parametrize(("path", "sha256", "size"), SETS, ids=["dev", "holdout"])
def test_every_item_is_complete(path: Path, sha256: str, size: int) -> None:
    items = load_items(path)
    assert len(items) == size and len({i["id"] for i in items}) == size
    for item in items:
        assert item["check"] in ("answer_contains", "answer_regex", "graceful_tool_failure")
        assert item["label"] in ("correct", "incorrect") and item["reason"].strip()
        assert item["answer"] and item["steps"][-1]["kind"] == "final_answer"
        if item["check"] != "graceful_tool_failure":
            assert item["params"], item["id"]


def test_holdout_shares_no_output_with_the_dev_set() -> None:
    def key(i: dict) -> tuple[str, str, str]:
        return (i["check"], i["input"], i["answer"])

    dev = load_items(CALIBRATION)
    holdout = load_items(HOLDOUT)
    assert not {key(i) for i in dev} & {key(i) for i in holdout}
    assert not {i["source"]["result_id"] for i in dev} & {i["source"]["result_id"] for i in holdout}


MUTANTS = CALIBRATION_DIR / "answer_checks_mutants_v1.yaml"
# The mutants' answer is part of what's pinned: it's generated, so the text is the label's basis.
FROZEN_MUTANTS_SHA256 = "7c3cbe4099c45700a0ae2df4ec290015de9d4167a083987937f45e40cfde55a9"


def test_mutants_are_frozen_and_regenerate_byte_for_byte() -> None:
    items = load_items(MUTANTS)
    canon = json.dumps(
        [[i["id"], i["check"], i["source"]["result_id"], i["label"], i["answer"]] for i in items],
        separators=(",", ":"),
    )
    assert hashlib.sha256(canon.encode()).hexdigest() == FROZEN_MUTANTS_SHA256
    regenerated = mutants.render(mutants.generate(load_items(HOLDOUT)))
    assert regenerated == MUTANTS.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_every_mutant_breaks_one_fact_of_a_holdout_answer() -> None:
    holdout = {i["id"]: i for i in load_items(HOLDOUT)}
    items = load_items(MUTANTS)
    assert len(items) == 30 and len({i["id"] for i in items}) == 30
    for item in items:
        original = holdout[item["mutation"]["from"]]
        assert item["label"] == "incorrect" and item["answer"] != original["answer"]
        assert original["label"] == "correct"
        assert item["steps"] == original["steps"] and item.get("params") == original.get("params")
        if item["check"] == "graceful_tool_failure":
            assert item["mutation"]["type"] in ("success_after_failure", "dropped_negation")


MUTANTS_HOLDOUT = CALIBRATION_DIR / "answer_checks_mutants_holdout_v1.yaml"
FROZEN_MUTANTS_HOLDOUT_SHA256 = "a76edad5873af7db7f0f72c0c07b6a8c5e7876eb294d6e60044e7aff84cebd46"


def test_holdout_mutants_are_frozen_new_and_regenerate_byte_for_byte() -> None:
    items = load_items(MUTANTS_HOLDOUT)
    canon = json.dumps(
        [[i["id"], i["check"], i["source"]["result_id"], i["label"], i["answer"]] for i in items],
        separators=(",", ":"),
    )
    assert hashlib.sha256(canon.encode()).hexdigest() == FROZEN_MUTANTS_HOLDOUT_SHA256
    assert mutants.render_holdout() == MUTANTS_HOLDOUT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert len(items) == 27 and all(i["label"] == "incorrect" for i in items)
    dev_mutants = {(i["check"], i["answer"]) for i in load_items(MUTANTS)}
    assert not dev_mutants & {(i["check"], i["answer"]) for i in items}
    originals = {i["id"]: i for i in load_items(CALIBRATION) + load_items(HOLDOUT)}
    for item in items:
        original = originals[item["mutation"]["from"]]
        assert original["label"] == "correct" and item["steps"] == original["steps"]
