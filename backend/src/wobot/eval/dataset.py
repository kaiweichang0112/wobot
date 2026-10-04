"""Evaluation datasets: versioned cases, each with the check that scores it, if any yet.

A case follows the contract in the design (case ID, scenario, user input, query time,
reference, expected behaviour) and adds a split and a check. Cases whose check cannot run
yet, such as those that need the answer agent of phase B, say why instead.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

DATASET_DIR = Path(__file__).resolve().parents[3] / "eval" / "datasets"
Split = Literal["dev", "heldout"]


@dataclass(frozen=True)
class Case:
    case_id: str
    scenario: str
    split: Split
    user_input: str
    expected_behavior: str
    reference: str | None  # the person's reference answer, for judge metrics later
    check: dict[str, Any] | None
    pending: str | None  # why the case is not scored yet
    # Earlier turns, scripted: what the user said, the tools then called and the answer.
    # The tools run for real when the case is played, so their IDs match the version.
    history: list[dict[str, Any]] = field(default_factory=list)
    chatbot_name: str | None = None  # the name the account gave its assistant


@dataclass(frozen=True)
class Dataset:
    name: str
    query_time: str
    timezone: str
    cases: list[Case]
    sha256: str  # of the file, recorded with each run


def load_dataset(name: str, directory: Path = DATASET_DIR) -> Dataset:
    path = directory / f"{name}.yaml"
    content = path.read_bytes()
    data = yaml.safe_load(content)
    cases = [
        Case(
            case_id=case["case_id"],
            scenario=case["scenario"],
            split=case["split"],
            user_input=case["user_input"],
            expected_behavior=case.get("expected_behavior", ""),
            reference=case.get("reference"),
            check=case.get("check"),
            pending=case.get("pending"),
            history=case.get("history", []),
            chatbot_name=case.get("chatbot_name"),
        )
        for case in data["cases"]
    ]
    if duplicates := {c.case_id for c in cases if [d.case_id for d in cases].count(c.case_id) > 1}:
        raise ValueError(f"{path.name}: repeated case IDs {sorted(duplicates)}")
    if bad := [c.case_id for c in cases if c.split not in ("dev", "heldout")]:
        raise ValueError(f"{path.name}: cases without a dev or heldout split: {bad}")
    return Dataset(
        name=data["dataset"],
        query_time=data["query_time"],
        timezone=data["timezone"],
        cases=cases,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def dataset_names(directory: Path = DATASET_DIR) -> list[str]:
    return sorted(path.stem for path in directory.glob("*.yaml"))
