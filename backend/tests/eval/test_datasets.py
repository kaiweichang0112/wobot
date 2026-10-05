"""The datasets and gold files in the repository read cleanly; matching them to records
needs a database, so `wobot-eval check-gold` does that part."""

import pytest

from wobot.agent.tools import build_tools
from wobot.eval import gold
from wobot.eval.agent import BEHAVIORS, RECOMMENDATION_CHECKS
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


def test_scripted_history_calls_fit_the_tools_as_they_are():
    # The history's tools run for real when a case is played: a call the tool's schema
    # refuses would end the case in an error, not measure it.
    tools = {tool.name: tool for tool in build_tools(db=None, embedder=None)}

    for name in dataset_names():
        for case in load_dataset(name).cases:
            for turn in case.history:
                for call in turn.get("tools", []):
                    schema = tools[call["name"]].tool_call_schema
                    schema.model_validate(call["args"])  # raises on a refused call


@pytest.mark.parametrize("name", dataset_names())
def test_behavior_checks_expect_only_known_behaviors(name):
    for case in load_dataset(name).cases:
        if case.check and case.check["kind"] in ("behavior", "recommendation"):
            known = set(BEHAVIORS)
            if case.check["kind"] == "recommendation":
                known |= set(RECOMMENDATION_CHECKS)
            assert set(case.check) - {"kind"} <= known, case.case_id
