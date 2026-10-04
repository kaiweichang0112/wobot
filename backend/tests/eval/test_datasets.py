"""The datasets and gold files in the repository read cleanly; matching them to records
needs a database, so `wobot-eval check-gold` does that part."""

import pytest

from wobot.agent.tools import build_tools
from wobot.eval import gold
from wobot.eval.dataset import dataset_names, load_dataset
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

    files = {path.name for path in gold.GOLD_DIR.iterdir() if path.name != "README.md"}

    assert files == used


def test_tool_labels_name_only_the_agents_tools():
    tools = {tool.name for tool in build_tools(db=None, embedder=None)}

    for name in dataset_names():
        for case in load_dataset(name).cases:
            if case.check and case.check["kind"] == "tools" and case.check["expect"]:
                named = {tool for expected in case.check["expect"] for tool in expected}
                assert named <= tools, (case.case_id, named - tools)
