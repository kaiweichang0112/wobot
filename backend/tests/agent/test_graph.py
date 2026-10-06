from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from wobot.agent.graph import (
    MAX_LIST_ROUNDS,
    MAX_REC_ROUNDS,
    build_graph,
    list_should_continue,
    ok_or_failed,
    pass_or_retry,
    rec_should_continue,
    route_by_intent,
    search_or_ask,
)

EXPECTED_GRAPH = Path(__file__).with_name("expected_graph.mmd")

CALLS_A_TOOL = AIMessage("", tool_calls=[{"name": "list_lectures", "args": {}, "id": "c1"}])


def test_the_graph_is_drawn_as_planned():
    # The plan's drawing is the contract: a change to a node or an edge shows here first.
    drawn = build_graph().get_graph().draw_mermaid()

    assert drawn == EXPECTED_GRAPH.read_text()


def test_every_conditional_edge_is_labelled():
    # A label named like its target is left out of the drawing.
    edges = build_graph().get_graph().edges

    unlabelled = [(e.source, e.target) for e in edges if e.conditional and not e.data]
    assert unlabelled == []


@pytest.mark.parametrize("route", ["chat", "knowledge", "list", "recommend"])
def test_the_route_names_the_path(route):
    assert route_by_intent({"route": route}) == route


@pytest.mark.parametrize(("status", "label"), [("ok", "ok"), ("failed", "failed")])
def test_a_failed_lookup_leaves_the_path(status, label):
    assert ok_or_failed({"status": status}) == label


@pytest.mark.parametrize(
    ("requirements", "label"),
    [
        (None, "ask"),
        ({"goal": "", "must_have": []}, "ask"),  # "recommend a product", no more
        ({"goal": "caring for a bedridden parent", "must_have": []}, "ask"),
        ({"goal": "caring for a bedridden parent", "must_have": [{"id": "n1"}]}, "search"),
    ],
)
def test_a_search_waits_for_a_goal_and_a_need(requirements, label):
    assert search_or_ask({"requirements": requirements}) == label


@pytest.mark.parametrize(
    ("should_continue", "key", "limit"),
    [
        (list_should_continue, "list_messages", MAX_LIST_ROUNDS),
        (rec_should_continue, "rec_messages", MAX_REC_ROUNDS),
    ],
)
def test_a_loop_runs_tools_until_the_agent_stops_or_the_rounds_run_out(should_continue, key, limit):
    assert should_continue({key: [CALLS_A_TOOL], "tool_rounds": 0}) == "tools"
    assert should_continue({key: [CALLS_A_TOOL], "tool_rounds": limit}) == "done"
    assert should_continue({key: [AIMessage("These will do.")], "tool_rounds": 0}) == "done"
    assert should_continue({key: [HumanMessage("all talks in 2024")], "tool_rounds": 0}) == "done"
    assert should_continue({key: [], "tool_rounds": 0}) == "done"


def test_a_decision_with_problems_goes_back_to_the_loop():
    assert pass_or_retry({"problems": ["n2 is unknown for p-1"]}) == "retry"
    assert pass_or_retry({"problems": []}) == "pass"
