"""The datasets and gold files in the repository read cleanly; matching them to records
needs a database, so `wobot-eval check-gold` does that part."""

import pytest

from wobot.agent.list_agent import TOOLS_BY_NAME
from wobot.eval import gold
from wobot.eval.dataset import dataset_names, load_dataset
from wobot.eval.judge_labels import read_labels
from wobot.eval.runner import case_refs

# A person's verdicts on replies a run kept, dev and held out: not of a case's sources.
JUDGE_LABELS = sorted(gold.GOLD_DIR.glob("judge-labels*.yaml"))


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

    kept = {"README.md", *(path.name for path in JUDGE_LABELS)}
    files = {path.name for path in gold.GOLD_DIR.iterdir() if path.name not in kept}

    assert files == used


@pytest.mark.parametrize("path", JUDGE_LABELS, ids=lambda path: path.name)
def test_the_judge_labels_read_cleanly(path):
    labels = read_labels(path)  # raises on a verdict other than pass or fail

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
