"""The datasets and gold files in the repository read cleanly; matching them to records
needs a database, so `wobot-eval check-gold` does that part."""

import pytest

from wobot.agent.list_agent import TOOLS_BY_NAME
from wobot.eval import gold
from wobot.eval.dataset import dataset_names, load_dataset
from wobot.eval.judge_labels import LABELS, read_labels
from wobot.eval.runner import case_refs


@pytest.mark.parametrize("name", dataset_names())
def test_every_case_reads_its_labels(name):
    for case in load_dataset(name).cases:
        if case.check and "gold" in case.check:
            case_refs(case, gold.GOLD_DIR, {})  # raises GoldError on a broken file


def test_every_gold_file_belongs_to_a_case():
    used = {
        case.check["gold"]["file"]
        for name in dataset_names()
        for case in load_dataset(name).cases
        if case.check and "gold" in case.check
    }

    # The judge's labels are of replies a run kept, not of a case's sources.
    files = {p.name for p in gold.GOLD_DIR.iterdir() if p.name not in ("README.md", LABELS.name)}

    assert files == used


def test_the_judge_labels_read_cleanly():
    labels = read_labels(LABELS)  # raises on a verdict other than pass or fail

    assert labels.entries and labels.blank == 0 and labels.labelled_on


@pytest.mark.parametrize("name", ["lists-v1", "turns-v1"])
def test_every_expected_list_call_is_one_the_tools_take(name):
    cases = [c for c in load_dataset(name).cases if c.check and "calls" in c.check]

    assert cases
    for case in cases:
        for accepted in case.check["calls"]:
            for call in accepted:
                filters = {name: value for name, value in call.items() if name != "tool"}
                spellings = filters.pop("contains", [None])
                for contains in spellings:
                    given = filters | ({} if contains is None else {"contains": contains})
                    TOOLS_BY_NAME[call["tool"]].model_validate(given)  # raises if refused
